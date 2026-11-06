"""Standard entry-signal dispatch for the isolated-instrument engine.

All pre-11:30 isolations use one standard execution mode named Path A.
It reuses the former Path B-style execution mechanics: the isolation-selected
bias instrument remains active and the dummy-order engine handles
post-isolation S/R routing and trade management. The index that triggered
isolation remains diagnostic metadata and does not select the execution path.

POST-11:30 remains a separate time-based regime. Its existing entry, target,
stop-loss and 15/15 Resistance exit behavior is preserved.
"""
G1 v4.1 — Dual-sided fast pivot + auto-reverse entry signal.

For the isolated NIFTY strike, this service:

1. Runs a fast pivot scan (left=15, right=3) on the bias side's
   candle series.
2. Decision tree:
     * Fast SUPPORT exists on bias side → ARM BIAS SIDE.
     * Fast RESISTANCE exists on bias side with NO support → TRIGGER
       AUTO-REVERSE to the counter side at the counter instrument's
       candle close at rejection time (or, as a last resort, its
       live LTP).
     * Nothing confirms inside the buffer window → HARD BLOCK
       "all_candidates_in_window_blocked".
3. Applies the LTP-tier stop-loss (unchanged from G1 v2).
4. First-to-confirm tiebreak uses `confirmed_time_fast`.

G1 v4.1 changes (2026-10-29)
----------------------------
Fixes the three bugs surfaced by the 2026-10-29 SIMULATE-mode replay:

  1. **Entry price source** — ``_entry_price_for()`` now falls back to
     a fresh ``HistoryV3Api.get_intra_day_candle_data`` fetch BEFORE
     falling back to the live LTP. This closes the "entry price is the
     15:31 wall-clock LTP instead of the 09:23 candle close" bug that
     appeared when the counter-side instrument was not yet registered
     with the SR engine (see ``level_isolated_instrument_service
     ._register_isolated_pair_with_sr``).

  2. **Auto-reverse price priority** — ``_trigger_auto_reverse()`` now
     uses ``_entry_price_for()``, so it prefers the counter-side candle
     close at ``bias_resistance_confirmed_time_fast`` over the live LTP.
     A degraded live-LTP fallback is still permitted (logged at WARNING)
     so a genuine "confirming bar not yet in the series" race in live
     mode does not hard-block the trade.

  3. **Target selection** — new ``_pick_target_resistance()`` helper
     returns the **first resistance confirmed strictly AFTER** the
     entry confirmation, among those priced above entry. Falls back to
     the lowest-above-entry resistance only when no time-ordered
     candidate exists. This replaces the previous "lowest price above
     entry" selection, which on a day-long down trend picked a
     resistance that only formed hours AFTER entry (e.g. 63.00 @ 15:24
     for a 09:23 entry, instead of the correct 110.65 @ 10:56).

G1 v4.2 changes (2026-10-29)
----------------------------
    * The standard session now uses one unified Path A execution mode,
      independent of whether NIFTY or an external index caused isolation:

        - Path A (``trigger_is_self_nifty == True``) — the trigger index
          was NIFTY itself; behaviour is **unchanged** from v4.1: the
          10-minute post-touch scan and auto-reverse both run.

        - Path B (``trigger_is_self_nifty == False``) — the trigger
          index was MIDCPNIFTY / BANKNIFTY / SENSEX. The 10-minute scan
          and auto-reverse are **intentionally disabled**. The
          bias-side instrument stays isolated all day, and the
          dummy-order service independently manages multi-order SR
          level execution + Rupee trailing on the bias-side instrument.

      The ``_build_unified_path_a_response()`` helper returns the common
      standard response. The POST-11:30 regime remains a separate
      dispatch evaluated before standard-session handling.

Post-11:30 regime — 2026-11-05
------------------------------
A new **POST-11:30 ISOLATED** regime sits between the Path B check
and the standard Path A logic.  The dispatch decision is made by
``evaluate_isolated_entry()`` (module-level function) using
``isolated_sr_tracker_service.get_active_isolated_state()["isolated_time"]``:

    * ``isolated_time.time() >= POST_1130_REGIME_TIME`` (11:30:00 IST)
        → the post-11:30 entry rule runs.  Opening-range / first-candle
          logic and the standard 10-minute post-touch scan are
          **bypassed entirely**.
    * otherwise
        → the existing Path A logic runs verbatim (unchanged).

The post-11:30 rule (``check_post_1130_entry``) applies the spec's
``(15, 30)`` entry-confirmation window:

    Condition 1: ``pivot_time < isolated_touch_time``
    Condition 2: ``confirmation_time >= isolated_time - 10 min buffer``

The first confirmed SR level that satisfies BOTH conditions wins.
When it wins, the response wrapper (``_build_post_1130_buy_response``)
reports ``execution_path = "POST_1130"`` and includes the standard
``POST_1130_TARGET_POINTS`` / ``POST_1130_STOPLOSS_PERCENT`` values so
downstream consumers can pick them up without re-reading config.

Post-11:30 refinements (2026-11-05, second pass)
------------------------------------------------
Four content changes close the gaps exposed by the 2026-11-05
backfill replay (isolated at 12:59, real level `support 99.50 @
confirmed 13:03`):

  A. **Defensive clamp** — ``_resolve_isolation_timestamps()`` now
     silently substitutes ``isolated_time = isolated_touch_time``
     whenever ``isolated_time > isolated_touch_time``, and logs the
     substitution exactly once per isolation. This makes the
     POST-1130 branch resilient even if the tracker's own backfill
     correction is bypassed or a stale state file survives a restart.

  B. **Deterministic ordering** — ``check_post_1130_entry()`` sorts
     ``sr_levels_15_30`` by ``confirmation_time`` ascending before
     iterating, so the "first confirmed level after isolation touch"
     rule is stable regardless of dict insertion order.

  C. **Support-only filter for CE bias** — a CE isolated from an R3
     touch is a "buy-the-bounce" setup; the meaningful POST-1130
     trigger is a **support** confirmation, not a resistance. The
     inverse holds for a PE isolated from an S3 touch (resistance-
     only). Auto-reverse is explicitly **not** part of the POST-1130
     spec and never fires here. For today's data this is what makes
     `support 99.50 @ 13:03` the correct and only match.

  D. **Enriched BUY payload** — ``_build_post_1130_buy_response()``
     now populates the ``entry`` block with everything the order
     placer needs without re-reading config:

         entry_price      — close of the confirmation candle
         entry_time       — ``confirmation_time``
         stop_price       — entry × (1 − POST_1130_STOPLOSS_PERCENT)
         target_price     — entry + POST_1130_TARGET_POINTS
         reference_type   — "support" | "resistance"
         reference_price  — numeric reference level price
         pivot_time       — matched level's pivot time
         confirmation_time — matched level's confirmation time

     and adds ``exit_sr_config = [15, 15]`` to the ``post_1130``
     block so the exit tracker knows which window to use.

G1 v4.1 flag-wiring fix (2026-10-29)
------------------------------------
Two v4.1 kill switches were defined in ``core/config.py`` but never
read by this service, so setting them to ``false`` in ``.env`` had no
effect. Both are now wired:

    * ``STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED`` (§5.B)
        ``_entry_price_for()`` now wraps the intraday-API fallback in
        this flag. When false, resolution falls straight through from
        the SR-engine candle buffer to live LTP, reproducing pre-v4.1
        behaviour for bisecting entry-price regressions.

    * ``STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY`` (§5.C)
        ``_pick_target_resistance()`` now gates its "confirmed strictly
        AFTER entry" filter on this flag. When false, the pre-v4.1
        "lowest price above entry" selection is restored.

Two additional flag **name** mismatches are corrected here as well.
The previous revision read:

    * ``STRATEGY_PRIORITIZE_SUPPORT_OVER_RESISTANCE``  (not in config.py)
    * ``STRATEGY_ALLOW_OPPOSITE_REVERSAL_ON_RESISTANCE``  (not in config.py)

which silently fell through the ``getattr(..., True)`` defaults. This
revision reads the exact keys that ``core/config.py`` defines:

    * ``STRATEGY_ENTRY_PRIORITIZE_SUPPORT``
    * ``STRATEGY_ENTRY_ALLOW_AUTO_REVERSE``

so operators can actually flip them off via ``.env``.

2026-11-05 dispatch-order fix (third pass)
------------------------------------------
``compute()`` now runs the POST-11:30 regime gate **before** the
Path B check.  The POST-1130 regime is keyed off ``isolated_time``
— not the trigger source — so a Path B isolation that happens at or
after 11:30 IST must route through POST-1130, superseding Path A/B.

Previously Path B won for every external-triggered isolation, which
is why the 2026-11-05 backfill replay never fired the correct CE
trade: the MIDCPNIFTY-triggered 12:59 isolation returned
``path_b_mode`` instead of a POST-1130 CE entry on support 99.50
@ 13:03.  With this reorder:

    * Pre-11:30 external isolation → Path B (unchanged).
    * Pre-11:30 self-NIFTY isolation → Path A (unchanged).
    * Post-11:30 isolation (any trigger) → POST_1130.
    * Post-11:30 external isolation with no matching SR → waits on
      ``waiting_post_1130`` and does NOT fall back to Path B.

The unified Path A response helper, the POST-1130 helpers, ``evaluate_isolated_entry``
and ``check_post_1130_entry`` are all unchanged.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, time as dt_time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Time helpers                                                                #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def _now_market() -> datetime:
    return datetime.now(_market_tz())

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        if n > 1e9:
            return datetime.fromtimestamp(n, tz=_market_tz())
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt.astimezone(_market_tz())

def _iso(dt: datetime) -> str:
    return dt.astimezone(_market_tz()).isoformat()

def _minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0

# --------------------------------------------------------------------------- #
# Touch-side parsing                                                          #
# --------------------------------------------------------------------------- #
def _parse_touch_reason(reason: Any) -> tuple[str | None, str | None]:
    r = str(reason or "").lower()
    if "r3" in r:
        return "R3", "CE"
    if "s3" in r:
        return "S3", "PE"
    return None, None

# --------------------------------------------------------------------------- #
# LTP-tier stop-loss (unchanged from G1 v2)                                   #
# --------------------------------------------------------------------------- #
_DEFAULT_STOP_TIERS: list[dict[str, Any]] = [
    {"max_ltp": 100.0, "sl_pct": 0.15},
    {"max_ltp": 200.0, "sl_pct": 0.12},
    {"max_ltp": 300.0, "sl_pct": 0.10},
    {"max_ltp": None,  "sl_pct": 0.05},
]

def _parse_stop_tiers() -> list[dict[str, Any]]:
    raw = (getattr(config, "STRATEGY_ENTRY_STOP_LTP_TIERS", "") or "").strip()
    if not raw:
        return _DEFAULT_STOP_TIERS
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[strategy-entry] STRATEGY_ENTRY_STOP_LTP_TIERS is not valid "
            "JSON; using default tiers."
        )
        return _DEFAULT_STOP_TIERS
    if not isinstance(parsed, list) or not parsed:
        return _DEFAULT_STOP_TIERS
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            sl_pct = float(item.get("sl_pct"))
        except (TypeError, ValueError):
            continue
        if sl_pct <= 0 or sl_pct >= 1:
            continue
        max_ltp_raw = item.get("max_ltp")
        max_ltp: float | None
        if max_ltp_raw is None:
            max_ltp = None
        else:
            try:
                max_ltp = float(max_ltp_raw)
            except (TypeError, ValueError):
                continue
            if max_ltp <= 0:
                continue
        out.append({"max_ltp": max_ltp, "sl_pct": sl_pct})
    if not out:
        return _DEFAULT_STOP_TIERS
    if not any(t.get("max_ltp") is None for t in out):
        last_sl = out[-1]["sl_pct"]
        out.append({"max_ltp": None, "sl_pct": last_sl})
    return out

def _tier_lookup(entry_price: float) -> float:
    tiers = _parse_stop_tiers()
    for tier in tiers:
        max_ltp = tier.get("max_ltp")
        if max_ltp is None or entry_price < max_ltp:
            return float(tier["sl_pct"])
    return float(tiers[-1]["sl_pct"]) if tiers else 0.15

def _get_tick_size(instrument_key: str) -> float:
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(instrument_key)
        if entry:
            ts = entry.get("tick_size")
            if ts is not None:
                ts_f = float(ts)
                if ts_f > 0:
                    return ts_f
    except Exception:
        pass
    return 0.05

def _round_to_tick(value: float, tick: float, mode: str) -> float:
    if tick <= 0:
        return float(value)
    mode = (mode or "down").lower()
    if mode == "up":
        return math.ceil(value / tick) * tick
    if mode == "nearest":
        return round(value / tick) * tick
    return math.floor(value / tick) * tick

def _compute_ltp_tier_stop(
    entry_price: float,
    instrument_key: str,
) -> tuple[float, float, float | None, str]:
    sl_pct = _tier_lookup(entry_price)
    hard_cap = float(getattr(config, "STRATEGY_MAX_STOP_PCT", 0.35))
    if sl_pct > hard_cap:
        logger.error(
            "[strategy-entry] sl_pct %.4f exceeds hard cap %.4f; clamping.",
            sl_pct, hard_cap,
        )
        sl_pct = hard_cap
    stop_raw = entry_price * (1.0 - sl_pct)
    tick = _get_tick_size(instrument_key)
    mode = str(getattr(config, "STRATEGY_ENTRY_STOP_TICK_ROUNDING", "down")).lower()
    stop = _round_to_tick(stop_raw, tick, mode)
    if stop <= 0:
        stop = stop_raw
    stop_pct = ((entry_price - stop) / entry_price) if entry_price > 0 else None
    return float(stop), float(sl_pct), (
        round(stop_pct, 6) if stop_pct is not None else None
    ), "ltp_tier"

# --------------------------------------------------------------------------- #
# Candle lookup + LTP                                                         #
# --------------------------------------------------------------------------- #
def _candle_close_at(instrument_key: str, iso_time: str) -> float | None:
    """Return the close of the 1-min candle at ``iso_time`` from the
    SR engine's in-memory state (per-instrument ``_states`` dict)."""
    target = _parse_iso(iso_time)
    if target is None:
        return None
    target_minute = target.replace(second=0, microsecond=0)
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
    except Exception:
        return None
    if not state:
        return None
    for candle in state.get("candles") or []:
        c_dt = _parse_iso(candle.get("time"))
        if c_dt is None:
            continue
        if c_dt.replace(second=0, microsecond=0) == target_minute:
            try:
                return float(candle["close"])
            except (KeyError, TypeError, ValueError):
                return None
    return None

def _candle_close_from_intraday(
    instrument_key: str,
    iso_time: str,
) -> float | None:
    """G1 v4.1 — fallback path when the SR engine has no candle state
    for ``instrument_key`` at ``iso_time``.

    Fetches today's intraday 1-min candles via the stateless
    ``live_sr_calc_service`` loader and returns the close of the bar
    whose minute matches ``iso_time``.

    This path is what makes the entry price correct in simulate mode
    when the counter-side instrument was not pre-registered with the
    SR engine (see the companion fix in
    ``level_isolated_instrument_service._register_isolated_pair_with_sr``,
    which now registers BOTH sides up front — the two fixes together
    guarantee a correct entry price even if one of them is bypassed).

    Never raises. Returns ``None`` on any failure.
    """
    target = _parse_iso(iso_time)
    if target is None:
        return None
    target_minute = target.replace(second=0, microsecond=0)
    try:
        from services.live_sr_calc_service import live_sr_calc_service
        candles = live_sr_calc_service.fetch_intraday_candles_for_entry(
            instrument_key
        )
    except Exception:
        logger.exception(
            "[strategy-entry] intraday fallback fetch failed for %s",
            instrument_key,
        )
        return None
    for c in candles or []:
        c_dt = _parse_iso(c.get("time"))
        if c_dt is None:
            continue
        if c_dt.replace(second=0, microsecond=0) == target_minute:
            try:
                return float(c["close"])
            except (KeyError, TypeError, ValueError):
                return None
    return None

def _live_ltp(instrument_key: str) -> float | None:
    try:
        from services.live_feed_service import live_feed_service
        ltp = live_feed_service.get_latest_ltp(instrument_key)
        if ltp is None:
            return None
        return float(ltp)
    except Exception:
        return None

def _entry_price_for(
    instrument_key: str,
    confirmed_time_iso: str,
) -> tuple[float | None, str | None]:
    """Resolve the entry price for an armed candidate.

    G1 v4.1 resolution order (strictly descending preference):

        1. ``candle_close_at_confirmed_fast`` — SR engine in-memory state
        2. ``candle_close_from_intraday``    — fresh intraday API fetch
        3. ``live_ltp``                      — last resort (logged WARNING
                                               by the caller because the
                                               resulting price may be
                                               wrong in simulate mode)

    Step (2) is gated on ``STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED``
    (default ``True``). Setting it to ``false`` in ``.env`` reproduces
    the pre-v4.1 path — skip the intraday fallback entirely and jump
    straight from the SR-engine buffer to live LTP. Useful for
    bisecting entry-price regressions without reverting the whole
    bundle.
    """
    close = _candle_close_at(instrument_key, confirmed_time_iso)
    if close is not None:
        return close, "candle_close_at_confirmed_fast"

    if bool(getattr(config, "STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED", True)):
        close = _candle_close_from_intraday(instrument_key, confirmed_time_iso)
        if close is not None:
            return close, "candle_close_from_intraday"

    ltp = _live_ltp(instrument_key)
    if ltp is not None:
        return ltp, "live_ltp"
    return None, None

# --------------------------------------------------------------------------- #
# Target resistance selection (G1 v4.1)                                       #
# --------------------------------------------------------------------------- #
def _pick_target_resistance(
    levels: list[dict[str, Any]],
    entry_price: float,
    entry_confirmed_iso: str,
) -> dict[str, Any] | None:
    """Return the target resistance for an armed entry.

    G1 v4.1 fix — the previous implementation picked the LOWEST-priced
    resistance above ``entry_price`` regardless of when it confirmed.
    On a day-long down trend this selected a resistance that only
    formed hours AFTER the entry (e.g. 63.00 @ 15:24 for a 09:23
    entry), which is not a real "next resistance" from the entry's
    perspective.

    Correct behaviour (when ``STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY``
    is True — the default):

        1. Filter to resistances that confirmed **strictly AFTER**
           ``entry_confirmed_iso``.
        2. Filter to resistances priced **strictly ABOVE**
           ``entry_price``.
        3. Pick the EARLIEST confirmed among the survivors.

    Fallback (always applied when the time filter is disabled or when
    no resistance survives it): return the lowest-priced resistance
    above entry. Returns ``None`` only when there is no resistance
    above ``entry_price`` at all.

    ``STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY`` is an opt-out kill
    switch for §5.C. Setting it to ``false`` in ``.env`` restores the
    pre-v4.1 "lowest above entry" behaviour, useful for bisecting
    target-selection regressions.
    """
    entry_conf_dt = _parse_iso(entry_confirmed_iso)
    require_post_entry = bool(getattr(
        config, "STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY", True
    ))

    # ---- Primary: first confirmed-after-entry resistance above price #.
    if require_post_entry and entry_conf_dt is not None:
        time_filtered: list[tuple[datetime, dict[str, Any]]] = []
        for lvl in levels:
            if str(lvl.get("level") or "").lower() != "resistance":
                continue
            try:
                r_price = float(lvl.get("price"))
            except (TypeError, ValueError):
                continue
            if r_price <= entry_price:
                continue
            r_conf_dt = _parse_iso(lvl.get("confirmed_time"))
            if r_conf_dt is None:
                continue
            if r_conf_dt <= entry_conf_dt:
                continue
            time_filtered.append((r_conf_dt, lvl))
        if time_filtered:
            time_filtered.sort(key=lambda t: t[0])
            return time_filtered[0][1]

    # ---- Fallback: lowest-priced resistance above entry -------------- #
    best: dict[str, Any] | None = None
    best_price: float | None = None
    for lvl in levels:
        if str(lvl.get("level") or "").lower() != "resistance":
            continue
        try:
            r_price = float(lvl.get("price"))
        except (TypeError, ValueError):
            continue
        if r_price <= entry_price:
            continue
        if best_price is None or r_price < best_price:
            best = lvl
            best_price = r_price
    return best

# --------------------------------------------------------------------------- #
# Fast pivot scan (unchanged from G1 v2)                                      #
# --------------------------------------------------------------------------- #
def _run_pivot_scan(
    instrument_key: str,
    left_bars: int,
    right_bars: int,
) -> list[dict[str, Any]]:
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
            if state is None:
                return []
            candles = list(state.get("candles") or [])
    except Exception:
        logger.exception(
            "[strategy-entry] fast scan failed to read candle series for %s",
            instrument_key,
        )
        return []

    if not candles:
        return []
    if len(candles) < left_bars + right_bars + 1:
        return []

    out: list[dict[str, Any]] = []
    n = len(candles)

    for i in range(left_bars, n - right_bars):
        c = candles[i]
        try:
            c_high = float(c["high"])
            c_low = float(c["low"])
            c_time = c["time"]
        except (KeyError, TypeError, ValueError):
            continue

        is_support = True
        for j in range(i - left_bars, i):
            try:
                if float(candles[j]["low"]) <= c_low:
                    is_support = False
                    break
            except (KeyError, TypeError, ValueError):
                is_support = False
                break
        if is_support:
            for j in range(i + 1, i + right_bars + 1):
                try:
                    if float(candles[j]["low"]) <= c_low:
                        is_support = False
                        break
                except (KeyError, TypeError, ValueError):
                    is_support = False
                    break

        if is_support:
            confirm_idx = i + right_bars
            try:
                confirmed_time = candles[confirm_idx]["time"]
            except (IndexError, KeyError, TypeError):
                continue
            out.append({
                "level": "support",
                "price": c_low,
                "pivot_bar_index": i,
                "pivot_time": c_time,
                "confirmed_bar_index": confirm_idx,
                "confirmed_time": confirmed_time,
                "left_bars": left_bars,
                "right_bars": right_bars,
            })

        is_resistance = True
        for j in range(i - left_bars, i):
            try:
                if float(candles[j]["high"]) >= c_high:
                    is_resistance = False
                    break
            except (KeyError, TypeError, ValueError):
                is_resistance = False
                break
        if is_resistance:
            for j in range(i + 1, i + right_bars + 1):
                try:
                    if float(candles[j]["high"]) >= c_high:
                        is_resistance = False
                        break
                except (KeyError, TypeError, ValueError):
                    is_resistance = False
                    break

        if is_resistance:
            confirm_idx = i + right_bars
            try:
                confirmed_time = candles[confirm_idx]["time"]
            except (IndexError, KeyError, TypeError):
                continue
            out.append({
                "level": "resistance",
                "price": c_high,
                "pivot_bar_index": i,
                "pivot_time": c_time,
                "confirmed_bar_index": confirm_idx,
                "confirmed_time": confirmed_time,
                "left_bars": left_bars,
                "right_bars": right_bars,
            })

    return out

# --------------------------------------------------------------------------- #
# Post-11:30 regime helpers (2026-11-05)                                      #
# --------------------------------------------------------------------------- #
def _regime_cutoff_time() -> dt_time:
    """Return the post-11:30 cutoff as a ``datetime.time`` (IST).

    Parsed from ``config.POST_1130_REGIME_TIME`` (default ``"11:30:00"``).
    Falls back to 11:30:00 on any parse failure so the regime gate is
    never silently disabled by a malformed env override.
    """
    raw = str(getattr(config, "POST_1130_REGIME_TIME", "11:30:00")).strip()
    try:
        parts = raw.split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        ss = int(parts[2]) if len(parts) > 2 else 0
        return dt_time(hh, mm, ss)
    except (ValueError, IndexError, AttributeError):
        return dt_time(11, 30, 0)

def _enrich_level_for_post_1130(level: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``level`` with the post-11:30 spec fields.

    Preserves the historical ``level`` (type string) so existing
    consumers keep working, and adds:

        ``level_price``        — float numeric price (mirrors ``price``)
        ``type``               — alias of ``level``
        ``confirmation_time``  — alias of ``confirmed_time``
        ``status``             — always ``"confirmed"`` (the pivot
                                 already passed the ``i + right_bars``
                                 check inside ``_run_pivot_scan``)
    """
    if not isinstance(level, dict):
        return {}
    out = dict(level)
    try:
        price_val = out.get("price")
        if price_val is None:
            price_val = out.get("level_price")
        out["level_price"] = float(price_val) if price_val is not None else None
    except (TypeError, ValueError):
        out["level_price"] = None
    out["type"] = out.get("type") or out.get("level")
    out["confirmation_time"] = (
        out.get("confirmation_time") or out.get("confirmed_time")
    )
    out.setdefault("status", "confirmed")
    return out

def _run_pivot_scan_enriched(
    instrument_key: str,
    left_bars: int,
    right_bars: int,
) -> list[dict[str, Any]]:
    """``_run_pivot_scan`` wrapped with post-11:30 enrichment.

    The result is directly consumable by ``check_post_1130_entry``
    (``pivot_time``, ``confirmation_time``, ``level``, ``level_price``,
    ``status``).
    """
    raw = _run_pivot_scan(instrument_key, left_bars, right_bars)
    return [_enrich_level_for_post_1130(lvl) for lvl in raw]

def evaluate_isolated_entry(
    instrument_data: dict[str, Any],
) -> dict[str, Any]:
    """Branch on ``isolated_time`` — the post-11:30 dispatcher.

    ``instrument_data`` is expected to carry:

        * ``isolated_time``        — ISO string (promotion timestamp)
        * ``isolated_touch_time``  — ISO string (may be None)
        * ``sr_levels_15_30``      — list of enriched level dicts
        * ``bias_type``            — "CE" / "PE" / None (used by the
                                     support-only filter)
        * ``instrument_key``       — for logging / diagnostics

    Returns one of:

        ``{"_branch": "standard"}``
            — pre-11:30 isolation.  ``StrategyEntrySignalService.compute()``
              falls through to the existing Path A logic.

        ``{"signal": "BUY", "strategy_regime": "POST_1130_ISOLATED", ...}``
            — post-11:30 with a matching confirmed SR level.

        ``{"_post_1130_waiting": True, "reason": "<why>"}``
            — post-11:30 but no matching level yet.  ``compute()``
              wraps this into a waiting-state response.

    This function has no side effects and can be called safely from
    any consumer that wants to evaluate the post-11:30 regime
    independently.
    """
    isolated_time_dt = _parse_iso(instrument_data.get("isolated_time"))
    isolated_touch_time_dt = _parse_iso(instrument_data.get("isolated_touch_time"))

    if isolated_time_dt is None:
        # No usable isolation timestamp — cannot determine regime.
        return check_standard_entry(instrument_data)

    cutoff = _regime_cutoff_time()
    if isolated_time_dt.time() >= cutoff:
        return check_post_1130_entry(
            instrument_data, isolated_time_dt, isolated_touch_time_dt,
        )

    return check_standard_entry(instrument_data)

def check_standard_entry(
    instrument_data: dict[str, Any],
) -> dict[str, Any]:
    """Marker for the standard pre-11:30 path.

    The standard (pre-11:30) entry logic is implemented inline in
    ``StrategyEntrySignalService.compute()`` — this function exists
    as a named branch in the spec's ``evaluate_isolated_entry``
    decision tree.  Returns a marker dict that the caller recognises
    to mean "fall through to the existing Path A logic".
    """
    _ = instrument_data
    return {"_branch": "standard"}

def _required_type_for_bias(bias_type: str | None) -> str | None:
    """Return the SR level type required for a POST-11:30 entry.

    CE bias (R3 touch) → ``"support"`` (buy-the-bounce)
    PE bias (S3 touch) → ``"resistance"``
    Unknown bias       → ``None`` (no filter)

    Rationale
    ---------
    A CE instrument is isolated because the underlying touched its R3
    — i.e. the underlying was rejected off resistance.  The post-1130
    entry rule is the confirmed **support** that price formed *before*
    the touch: a "buy-the-dip" setup on the option premium.

    A PE instrument is the mirror case — isolated on an S3 touch
    (rejected off support); the entry rule waits for a **resistance**
    the premium formed before the touch.

    Auto-reverse (resistance → PE inside a CE bias, or vice versa) is
    **not** part of the post-1130 spec and never fires here — the
    spec's condition pair (pivot < touch, confirm ≥ min_confirm)
    already filters out post-touch resistances naturally.
    """
    b = str(bias_type or "").strip().upper()
    if b == "CE":
        return "support"
    if b == "PE":
        return "resistance"
    return None

def check_post_1130_entry(
    instrument_data: dict[str, Any],
    isolated_time: datetime,
    isolated_touch_time: datetime | None,
) -> dict[str, Any]:
    """Post-11:30 condition validator — spec §2.1 + §2.2.

    Iterates the ``(15, 30)``-confirmed SR levels supplied in
    ``instrument_data["sr_levels_15_30"]`` **sorted by
    ``confirmation_time`` ascending**, and returns the FIRST level
    that satisfies ALL of:

        Condition 1: ``pivot_time < isolated_touch_time``
        Condition 2: ``confirmation_time >= isolated_time - 10 min buffer``
        Type filter: (2026-11-05, second pass) if ``bias_type`` is
                     present, require ``support`` for CE and
                     ``resistance`` for PE.  ``bias_type`` is
                     resolved from the isolation reason upstream.

    The deterministic ordering (sort by confirmation_time) is what
    makes the rule stable regardless of dict insertion order — the
    "first confirmed level after isolation touch" is a well-defined
    sequence, not an accident of iteration order.

    Returns the signal dict on match; otherwise returns a
    ``{"_post_1130_waiting": True, ...}`` marker so the caller can
    distinguish "waiting for a matching level" from a hard block.

    Notes
    -----
    * ``POST_1130_BUFFER_MINUTES`` (default 10) is read from config
      on every call so operators can tune it without restarting the
      service.
    * ``POST_1130_TARGET_POINTS`` (default 50.0) and
      ``POST_1130_STOPLOSS_PERCENT`` (default 0.30) are echoed into
      the returned signal so downstream consumers do not need to
      re-read config.
    * When ``isolated_touch_time`` is None the caller has already
      fallen back to ``isolated_time`` in ``_build_instrument_data``;
      if it is still None here, condition 1 cannot be verified and the
      caller is told via ``reason = "missing_touch_time"``.
    * When ``bias_type`` is missing/unrecognised the type filter is
      skipped and a WARNING is logged once — this preserves backward
      compatibility for callers that haven't yet wired the field.
    """
    sr_15_30 = instrument_data.get("sr_levels_15_30") or []
    if not isinstance(sr_15_30, list):
        sr_15_30 = []

    if not sr_15_30:
        return {"_post_1130_waiting": True, "reason": "no_sr_levels_yet"}

    buffer_min = int(getattr(config, "POST_1130_BUFFER_MINUTES", 10))
    min_confirm_time = isolated_time - timedelta(minutes=buffer_min)

    if isolated_touch_time is None:
        return {"_post_1130_waiting": True, "reason": "missing_touch_time"}

    # ---- 2026-11-05 (B): deterministic ordering -------------------- #
    # Sort by confirmation_time ascending so the "first confirmed
    # level after isolation touch" rule is stable.  Levels whose
    # confirmation_time cannot be parsed are pushed to the end —
    # they can never satisfy condition 2 anyway.
    _tz = _market_tz()
    _dt_min = datetime.min.replace(tzinfo=_tz)
    ordered = sorted(
        (sr for sr in sr_15_30 if isinstance(sr, dict)),
        key=lambda sr: (
            _parse_iso(sr.get("confirmation_time") or sr.get("confirmed_time"))
            or _dt_min
        ),
    )

    # ---- 2026-11-05 (C): support-only / resistance-only filter ---- #
    bias_type = instrument_data.get("bias_type")
    required_type = _required_type_for_bias(bias_type)
    if required_type is None:
        logger.warning(
            "[strategy-entry] POST-1130 bias_type=%r unrecognised — "
            "skipping the support-only / resistance-only type filter "
            "for this evaluation",
            bias_type,
        )

    # Diagnostics counters (informational — surfaced via the waiting
    # response when no match is found so the operator can see WHY).
    time_valid_count = 0
    type_filtered_count = 0

    for sr in ordered:
        if sr.get("status") != "confirmed":
            continue

        pivot_dt = _parse_iso(sr.get("pivot_time"))
        confirm_dt = _parse_iso(
            sr.get("confirmation_time") or sr.get("confirmed_time")
        )
        if pivot_dt is None or confirm_dt is None:
            continue

        # Condition 1 — pivot must be strictly BEFORE the touch.
        cond_pivot = pivot_dt < isolated_touch_time

        # Condition 2 — confirmation must be inside the buffer window.
        cond_confirm = confirm_dt >= min_confirm_time

        if not (cond_pivot and cond_confirm):
            continue

        time_valid_count += 1

        # Type filter — CE → support, PE → resistance.
        if required_type is not None:
            level_type = str(sr.get("level") or "").strip().lower()
            if level_type != required_type:
                type_filtered_count += 1
                logger.debug(
                    "[strategy-entry] POST-1130 skipped level — "
                    "type=%s (required=%s) pivot=%s confirm=%s",
                    level_type,
                    required_type,
                    sr.get("pivot_time"),
                    sr.get("confirmation_time") or sr.get("confirmed_time"),
                )
                continue

        ref_price = sr.get("level_price")
        if ref_price is None:
            ref_price = sr.get("price")
        logger.info(
            "[strategy-entry] POST-1130 match — type=%s price=%s "
            "pivot=%s confirm=%s (buffer=%s min, min_confirm=%s, "
            "bias=%s required_type=%s)",
            sr.get("level"),
            ref_price,
            sr.get("pivot_time"),
            sr.get("confirmation_time") or sr.get("confirmed_time"),
            buffer_min,
            _iso(min_confirm_time),
            bias_type,
            required_type,
        )
        return {
            "signal": "BUY",
            "strategy_regime": "POST_1130_ISOLATED",
            "sr_reference_level": sr.get("level"),
            "sr_reference_price": ref_price,
            "pivot_time": sr.get("pivot_time"),
            "confirmation_time": sr.get("confirmation_time")
            or sr.get("confirmed_time"),
            "target_points": float(
                getattr(config, "POST_1130_TARGET_POINTS", 50.0)
            ),
            "stoploss_pct": float(
                getattr(config, "POST_1130_STOPLOSS_PERCENT", 0.30)
            ),
            "buffer_minutes": buffer_min,
            "required_type": required_type,
            "bias_type": bias_type,
            "_matched_level": sr,
        }

    # ---- No match — build a specific waiting reason ---------------- #
    waiting: dict[str, Any] = {
        "_post_1130_waiting": True,
        "reason": "no_matching_sr_level",
        "time_valid_count": time_valid_count,
        "type_filtered_count": type_filtered_count,
        "required_type": required_type,
        "bias_type": bias_type,
    }
    if time_valid_count == 0:
        waiting["reason"] = "no_matching_sr_level"
    elif required_type is not None and time_valid_count == type_filtered_count:
        # All time-valid levels were filtered out by type.
        waiting["reason"] = "no_matching_sr_level_of_required_type"
    return waiting

# --------------------------------------------------------------------------- #
# Derive display confirmed time                                               #
# --------------------------------------------------------------------------- #
def _derive_display_confirmed_time(
    instrument_key: str,
    pivot_bar_index: int,
) -> str | None:
    if pivot_bar_index is None or pivot_bar_index < 0:
        return None
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
            if state is None:
                return None
            candles = list(state.get("candles") or [])
    except Exception:
        return None
    display_right = int(getattr(config, "LIVE_SR_RIGHT_BARS", 3))
    confirm_idx = pivot_bar_index + display_right
    if confirm_idx < 0 or confirm_idx >= len(candles):
        return None
    try:
        return str(candles[confirm_idx]["time"])
    except (KeyError, TypeError):
        return None

# --------------------------------------------------------------------------- #
# Both sides lookup                                                           #
# --------------------------------------------------------------------------- #
def _both_sides(
    underlying: str,
    strike: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        from services.instrument_registry import instrument_registry
    except Exception:
        return None, None
    ce = instrument_registry.resolve(underlying, strike, "CE")
    pe = instrument_registry.resolve(underlying, strike, "PE")
    return ce, pe

# --------------------------------------------------------------------------- #
# Candidate factories                                                         #
# --------------------------------------------------------------------------- #
def _empty_candidate(
    side: str,
    instrument_key: str | None,
    trading_symbol: str | None,
    is_bias_side: bool,
) -> dict[str, Any]:
    return {
        "side": side,
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "is_bias_side": is_bias_side,
        "state": "waiting",
        "state_note": None,
        "entry_type": None,
        "entry_support": None,
        "blocking_resistance": None,
        "reversal_trigger": None,
        "entry": None,
        "warning": None,
        "warning_detail": None,
    }

def _compute_candidate(
    side: str,
    entry: dict[str, Any] | None,
    is_bias_side: bool,
    touch_dt: datetime,
    window_end: datetime,
    now: datetime,
    counter_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """G1 v3.2 — bias-side dual scan + auto-reverse.

    Decision priority on the bias side:
      1. Fast SUPPORT in window → ARM BIAS (highest priority).
      2. Fast RESISTANCE in window with NO support → AUTO-REVERSE.
      3. Window expired → BLOCK.

    When called with ``is_bias_side == False`` (counter side), the
    candidate is filled in by the auto-reverse branch of the bias-side
    candidate rather than being scanned independently.
    """
    if entry is None:
        return _empty_candidate(side, None, None, is_bias_side)

    key = str(entry.get("instrument_key") or "").strip()
    symbol = entry.get("trading_symbol")
    cand = _empty_candidate(side, key, symbol, is_bias_side)

    # ---- Auto-reverse path: not the bias side --------------------- #
    if not is_bias_side:
        # Marker candidate — populated by the bias-side auto-reverse
        # branch. Left in "waiting" until then.
        return cand

    left_bars = int(getattr(
        config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS", config.LIVE_SR_LEFT_BARS
    ))
    right_bars = int(getattr(config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3))
    levels = _run_pivot_scan(key, left_bars, right_bars)

    if not levels:
        cand["state_note"] = "no_sr_levels"
        return cand

    # ---- 1. Collect supports + resistances in the window --------- #
    supports_in_window: list[dict[str, Any]] = []
    resistances_in_window: list[dict[str, Any]] = []
    for lvl in levels:
        p_dt = _parse_iso(lvl.get("pivot_time"))
        if p_dt is None:
            continue
        if p_dt < touch_dt or p_dt > window_end:
            continue
        try:
            price = float(lvl.get("price"))
        except (TypeError, ValueError):
            continue
        rec = {
            "price": price,
            "pivot_time": _iso(p_dt),
            "confirmed_time_fast": lvl.get("confirmed_time"),
            "pivot_bar_index": lvl.get("pivot_bar_index"),
            "pivot_dt": p_dt,
            "raw": lvl,
        }
        if str(lvl.get("level") or "").lower() == "support":
            supports_in_window.append(rec)
        elif str(lvl.get("level") or "").lower() == "resistance":
            resistances_in_window.append(rec)

    supports_in_window.sort(key=lambda s: s["pivot_dt"])
    resistances_in_window.sort(key=lambda r: r["pivot_dt"])

    # ---- 2. Prioritize support over resistance ------------------- #
    # G1 v4.1 flag-wiring fix — read the exact keys that
    # ``core/config.py`` defines. The previous revision read
    # ``STRATEGY_PRIORITIZE_SUPPORT_OVER_RESISTANCE`` and
    # ``STRATEGY_ALLOW_OPPOSITE_REVERSAL_ON_RESISTANCE``, neither of
    # which exists in config.py; both ``getattr`` calls silently fell
    # through to ``True`` regardless of the operator's .env setting.
    prioritize_support = bool(getattr(
        config, "STRATEGY_ENTRY_PRIORITIZE_SUPPORT", True
    ))
    allow_auto_reverse = bool(getattr(
        config, "STRATEGY_ENTRY_ALLOW_AUTO_REVERSE", True
    ))

    # ---- CASE 1: SUPPORT (priority) ------------------------------ #
    if supports_in_window and (prioritize_support or not resistances_in_window):
        return _arm_bias_on_support(
            cand, key, symbol, supports_in_window, levels,
            left_bars, right_bars, now,
        )

    # ---- CASE 2: RESISTANCE only (auto-reverse) ------------------ #
    if (
        resistances_in_window
        and not supports_in_window
        and allow_auto_reverse
        and counter_entry is not None
    ):
        return _trigger_auto_reverse(
            cand, key, symbol, resistances_in_window,
            counter_entry, now,
        )

    # ---- CASE 3: window still open, nothing yet ------------------ #
    if now <= window_end:
        cand["state"] = "waiting"
        cand["state_note"] = (
            "no_support_pivot_in_window" if not supports_in_window
            else "waiting_for_fast_confirmation"
        )
        return cand

    # ---- CASE 4: window expired ---------------------------------- #
    cand["state"] = "blocked"
    cand["state_note"] = "all_candidates_in_window_blocked"
    return cand

# --------------------------------------------------------------------------- #
# Case 1: arm bias on support (target selection G1 v4.1)                     #
# --------------------------------------------------------------------------- #
def _arm_bias_on_support(
    cand: dict[str, Any],
    key: str,
    symbol: str | None,
    supports_in_window: list[dict[str, Any]],
    levels: list[dict[str, Any]],
    left_bars: int,
    right_bars: int,
    now: datetime,
) -> dict[str, Any]:
    for s in supports_in_window:
        support_pivot_dt = s["pivot_dt"]
        support_confirmed_fast_iso = s["confirmed_time_fast"]
        support_confirmed_fast_dt = _parse_iso(support_confirmed_fast_iso)
        if support_confirmed_fast_dt is None:
            continue

        # Resistance blocker (unchanged rule, fast window)
        blocker: dict[str, Any] | None = None
        if getattr(config, "STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE", True):
            for lvl in levels:
                if str(lvl.get("level") or "").lower() != "resistance":
                    continue
                r_pivot_dt = _parse_iso(lvl.get("pivot_time"))
                r_conf_dt = _parse_iso(lvl.get("confirmed_time"))
                if r_pivot_dt is None or r_conf_dt is None:
                    continue
                try:
                    r_price = float(lvl.get("price"))
                except (TypeError, ValueError):
                    continue
                if r_price <= s["price"]:
                    continue
                if r_pivot_dt <= support_pivot_dt:
                    continue
                if r_conf_dt > support_confirmed_fast_dt:
                    continue
                if blocker is None or r_price < float(blocker.get("price") or 0.0):
                    blocker = {
                        "price": r_price,
                        "pivot_time": _iso(r_pivot_dt),
                        "confirmed_time": lvl.get("confirmed_time"),
                    }
        if blocker is not None:
            cand["blocking_resistance"] = blocker
            cand["state_note"] = "all_supports_blocked_so_far"
            continue

        display_confirmed_iso = _derive_display_confirmed_time(
            key, s["pivot_bar_index"] or -1,
        )
        entry_support = {
            "price": s["price"],
            "pivot_time": s["pivot_time"],
            "confirmed_time_fast": support_confirmed_fast_iso,
            "confirmed_time": display_confirmed_iso,
        }
        cand["entry_support"] = entry_support
        cand["blocking_resistance"] = None

        if support_confirmed_fast_dt > now:
            cand["state"] = "waiting_confirmation"
            cand["state_note"] = "support_found_waiting_for_confirmation"
            cand["seconds_until_confirmation"] = int(
                (support_confirmed_fast_dt - now).total_seconds()
            )
            return cand

        entry_price, price_source = _entry_price_for(
            key, support_confirmed_fast_iso,
        )
        if entry_price is None:
            cand["state"] = "blocked"
            cand["state_note"] = "no_entry_price"
            return cand
        if entry_price <= 0:
            cand["state"] = "blocked"
            cand["state_note"] = "invalid_entry_price"
            return cand
        if entry_price < s["price"]:
            cand["state"] = "blocked"
            cand["state_note"] = "entry_below_support"
            return cand

        stop_price, sl_pct, stop_pct, stop_source = _compute_ltp_tier_stop(
            entry_price, key,
        )

        warning = None
        warning_detail = None
        support_gap_pct = (entry_price - s["price"]) / entry_price
        if support_gap_pct > 0.20:
            warning = "support_far_below_entry"
            warning_detail = (
                f"Support {s['price']:.2f} is "
                f"{support_gap_pct * 100:.1f}% below entry "
                f"{entry_price:.2f}."
            )

        # G1 v4.1 — target: first resistance confirmed AFTER the entry
        # support's confirmed_time_fast, priced above entry_price.
        # Filter is gated on STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY
        # inside ``_pick_target_resistance``.
        best_r = _pick_target_resistance(
            levels, entry_price, support_confirmed_fast_iso,
        )
        target_price = None
        target_pivot_time = None
        if best_r is not None:
            try:
                target_price = float(best_r.get("price"))
            except (TypeError, ValueError):
                target_price = None
            target_pivot_time = best_r.get("pivot_time")

        cand["state"] = "armed"
        cand["state_note"] = "entry_support_confirmed_fast"
        cand["entry_type"] = "bias_support_bounce"
        cand["warning"] = warning
        cand["warning_detail"] = warning_detail
        cand["entry"] = {
            "price": entry_price,
            "price_source": price_source,
            "time": support_confirmed_fast_iso,
            "stop": stop_price,
            "stop_pct": stop_pct,
            "sl_pct": sl_pct,
            "stop_source": stop_source,
            "target": target_price,
            "target_pivot_time": target_pivot_time,
        }
        return cand

    cand["state"] = "blocked"
    cand["state_note"] = "all_supports_in_window_blocked"
    return cand

# --------------------------------------------------------------------------- #
# Case 2: auto-reverse on bias-side rejection (G1 v4.1 fixes)                 #
# --------------------------------------------------------------------------- #
def _trigger_auto_reverse(
    cand: dict[str, Any],
    key: str,
    symbol: str | None,
    resistances_in_window: list[dict[str, Any]],
    counter_entry: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Bias side confirmed a fast resistance with no support → flip.

    G1 v4.1 changes vs the previous revision
    ----------------------------------------
    * The counter-side entry price is now resolved via
      ``_entry_price_for()`` — i.e. the **candle close at the rejection
      minute** is preferred, with a fresh intraday fetch as fallback,
      and live LTP **only as a last resort** (logged WARNING, marked
      degraded in the entry block). The previous revision preferred
      the live LTP, which in simulate mode gave the wall-clock price
      (e.g. 61.65 at 15:31) instead of the historical price at the
      rejection moment (e.g. 186.55 at 09:23).

    * The target is now selected via ``_pick_target_resistance()``
      (first resistance confirmed AFTER the rejection), matching the
      bias-side arm path. The post-entry filter is gated on
      ``STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY``.
    """
    for r in resistances_in_window:
        r_conf_iso = r["confirmed_time_fast"]
        r_conf_dt = _parse_iso(r_conf_iso)
        if r_conf_dt is None:
            continue
        if r_conf_dt > now:
            cand["state"] = "waiting_confirmation"
            cand["state_note"] = "resistance_found_awaiting_auto_reverse"
            cand["seconds_until_confirmation"] = int(
                (r_conf_dt - now).total_seconds()
            )
            cand["blocking_resistance"] = {
                "price": r["price"],
                "pivot_time": r["pivot_time"],
                "confirmed_time": r_conf_iso,
            }
            return cand

        counter_key = str(counter_entry.get("instrument_key") or "")
        counter_symbol = counter_entry.get("trading_symbol")

        # G1 v4.1 — resolve counter price via _entry_price_for() so the
        # candle close at rejection time wins over the live LTP.
        counter_price, price_source = _entry_price_for(
            counter_key, r_conf_iso,
        )

        # Log the degraded fallback so simulate-mode drift is visible.
        if counter_price is not None and price_source == "live_ltp":
            logger.warning(
                "[strategy-entry] counter-side entry price for %s fell "
                "back to live_ltp (no candle at %s in SR state or "
                "intraday fetch) — entry price may be wrong in "
                "simulate mode",
                counter_key, r_conf_iso,
            )

        if counter_price is None or counter_price <= 0:
            cand["state"] = "blocked"
            cand["state_note"] = "invalid_counter_price"
            return cand

        stop_price, sl_pct, stop_pct, stop_source = _compute_ltp_tier_stop(
            counter_price, counter_key,
        )

        # Target: first counter-side resistance confirmed AFTER the
        # rejection minute, priced above the counter entry. Post-entry
        # filter gated on STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY.
        counter_levels = _run_pivot_scan(
            counter_key,
            int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS",
                config.LIVE_SR_LEFT_BARS,
            )),
            int(getattr(config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3)),
        )
        best_r = _pick_target_resistance(
            counter_levels, counter_price, r_conf_iso,
        )
        target_price = None
        target_pivot_time = None
        if best_r is not None:
            try:
                target_price = float(best_r.get("price"))
            except (TypeError, ValueError):
                target_price = None
            target_pivot_time = best_r.get("pivot_time")

        cand["state"] = "armed"
        cand["state_note"] = "auto_reverse_on_bias_rejection"
        cand["entry_type"] = "auto_reverse_on_rejection"
        cand["reversal_trigger"] = {
            "bias_resistance_price": r["price"],
            "bias_resistance_pivot_time": r["pivot_time"],
            "bias_resistance_confirmed_time_fast": r_conf_iso,
            "rejected_side": (
                "CE" if str(cand.get("side") or "").upper() == "CE"
                else "PE"
            ),
        }
        # The actual armed instrument is the COUNTER side.
        cand["armed_instrument_key"] = counter_key
        cand["armed_trading_symbol"] = counter_symbol
        cand["armed_side"] = "PE" if str(cand.get("side") or "").upper() == "CE" else "CE"
        cand["entry"] = {
            "price": counter_price,
            "price_source": price_source,
            "time": r_conf_iso,
            "stop": stop_price,
            "stop_pct": stop_pct,
            "sl_pct": sl_pct,
            "stop_source": stop_source,
            "target": target_price,
            "target_pivot_time": target_pivot_time,
        }
        return cand

    cand["state"] = "blocked"
    cand["state_note"] = "auto_reverse_unresolvable"
    return cand

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class StrategyEntrySignalService:
    def __init__(self) -> None:
        # 2026-11-05 (A) — one-shot INFO logging keyed by instrument_key.
        # Prevents a noisy loop of identical "clamped isolated_time"
        # log lines every scheduler tick.
        self._clamped_isolation_keys: set[str] = set()

    # ------------------------------------------------------------------ #
    # Post-11:30 helpers (2026-11-05)                                    #
    # ------------------------------------------------------------------ #
    def _resolve_isolation_timestamps(
        self,
        isolated: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Return ``(isolated_time, isolated_touch_time)`` for the
        current isolated instrument.

        Preference order:

            1. ``isolated_sr_tracker_service.get_active_isolated_state()``
               — the authoritative source once the tracker has taken
               over the isolation.  Provides BOTH timestamps when
               available.
            2. Isolation record's ``isolated_at`` / ``touched_at``
               — populated by ``level_isolated_instrument_service``
               and always present.
            3. Best-effort fallback: touch = isolation (they happen
               together on the isolation tick).

        **2026-11-05 defensive clamp (A)** — after resolution, if the
        raw ``isolated_time`` parses and is strictly greater than the
        raw ``isolated_touch_time``, the function silently substitutes
        ``isolated_time = isolated_touch_time`` and logs the
        substitution exactly once per isolation (keyed by
        ``instrument_key``).

        This is a belt-and-suspenders guard on top of the tracker-side
        correction: even if the tracker's own backfill fix is
        bypassed, or a state file written by the buggy build survives
        a restart, the POST-1130 buffer check still anchors against
        the real market moment (e.g. 12:59) instead of the process-
        start timestamp (e.g. 23:27).

        Both values are returned as raw strings (ISO or numeric) — the
        caller parses them via ``_parse_iso``.  ``None`` is possible
        only when neither source has a value, in which case the
        post-11:30 gate is skipped (see ``evaluate_isolated_entry``).
        """
        iso_time_raw = None
        iso_touch_raw = None

        try:
            from services.isolated_sr_tracker_service import (
                isolated_sr_tracker_service,
            )
            tracker_state = isolated_sr_tracker_service.get_active_isolated_state()
            if tracker_state and str(
                tracker_state.get("instrument_key") or ""
            ) == str(isolated.get("instrument_key") or ""):
                iso_time_raw = tracker_state.get("isolated_time")
                iso_touch_raw = tracker_state.get("isolated_touch_time")
        except Exception:
            logger.exception(
                "[strategy-entry] tracker timestamp fetch failed for %s",
                isolated.get("instrument_key"),
            )

        if not iso_time_raw:
            iso_time_raw = (
                isolated.get("isolated_at") or isolated.get("touched_at")
            )
        if not iso_touch_raw:
            iso_touch_raw = (
                isolated.get("touched_at") or isolated.get("isolated_at")
            )

        # ---- 2026-11-05 (A): defensive clamp ---------------------- #
        # If the raw isolated_time parses and is LATER than the raw
        # touch time, prefer the touch time as the effective event
        # moment.  This is the last line of defence against the
        # backfill-replay pollution bug.
        if iso_time_raw and iso_touch_raw:
            iso_dt = _parse_iso(iso_time_raw)
            touch_dt = _parse_iso(iso_touch_raw)
            if iso_dt is not None and touch_dt is not None and iso_dt > touch_dt:
                key = str(isolated.get("instrument_key") or "")
                if key and key not in self._clamped_isolation_keys:
                    self._clamped_isolation_keys.add(key)
                    logger.info(
                        "[strategy-entry] clamping isolated_time for "
                        "POST-1130 eval — key=%s stored=%s touch=%s "
                        "(gap=%.1f min) → using touch as effective "
                        "isolated_time",
                        key,
                        iso_time_raw,
                        iso_touch_raw,
                        (iso_dt - touch_dt).total_seconds() / 60.0,
                    )
                iso_time_raw = iso_touch_raw
        # ------------------------------------------------------------- #

        return iso_time_raw, iso_touch_raw

    def _build_instrument_data(
        self,
        isolated: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the ``instrument_data`` dict for ``evaluate_isolated_entry``.

        Fetches the ``(15, 30)``-configured SR levels via
        ``_run_pivot_scan_enriched`` so the caller receives
        post-11:30-spec-shaped level dicts (``level_price``,
        ``type``, ``confirmation_time``, ``status``) in addition
        to the historical fields.

        Returns an empty ``sr_levels_15_30`` when the instrument is
        not yet in the live SR engine's candle state — the caller
        treats that as "waiting for candles".

        2026-11-05 additions
        --------------------
        * ``bias_type`` — "CE" / "PE" / None, derived from the
          isolation reason.  Consumed by ``check_post_1130_entry``
          for the support-only / resistance-only filter (change C).
        * ``isolated_time`` / ``isolated_touch_time`` are the values
          returned by ``_resolve_isolation_timestamps`` — i.e. already
          backfill-corrected (change A).
        """
        instrument_key = str(isolated.get("instrument_key") or "").strip()
        iso_time_raw, iso_touch_raw = self._resolve_isolation_timestamps(isolated)

        # Bias side from the isolation reason (R3 → CE, S3 → PE).
        _touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))

        entry_config = getattr(config, "POST_1130_ENTRY_SR_CONFIG", (15, 30))
        try:
            entry_left = max(1, int(entry_config[0]))
            entry_right = max(1, int(entry_config[1]))
        except (TypeError, ValueError, IndexError):
            entry_left, entry_right = 15, 30

        sr_15_30: list[dict[str, Any]] = []
        if instrument_key:
            sr_15_30 = _run_pivot_scan_enriched(
                instrument_key, entry_left, entry_right,
            )

        return {
            "instrument_key": instrument_key,
            "isolated_time": iso_time_raw,
            "isolated_touch_time": iso_touch_raw,
            "sr_levels_15_30": sr_15_30,
            "entry_sr_config": [entry_left, entry_right],
            "bias_type": bias_type,
        }

    def _build_post_1130_buy_response(
        self,
        branch: dict[str, Any],
        isolated: dict[str, Any],
        iso_status: dict[str, Any],
        instrument_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Wrap a post-11:30 ``{"signal": "BUY", ...}`` into the
        standard response shape so downstream consumers can parse it
        uniformly.

        Includes a ``post_1130`` block carrying the reference SR
        level, the ``(15, 30)`` config actually used, the target /
        stop values, AND (2026-11-05, change D) the ``(15, 15)``
        exit-SR config that the exit tracker needs.

        The enriched ``entry`` block carries everything the order
        placer needs without re-reading config:

            entry_price       — close of the confirmation candle
                                (resolved via ``_entry_price_for``)
            entry_time        — ``confirmation_time`` of the
                                matched level
            stop_price        — entry × (1 − POST_1130_STOPLOSS_PERCENT)
            target_price      — entry + POST_1130_TARGET_POINTS
            reference_type    — "support" | "resistance"
            reference_price   — numeric reference level price
            pivot_time        — matched level's pivot time
            confirmation_time — matched level's confirmation time

        The advisory entry / stop / target values are informational
        only — ``dummy_order_service`` resolves its own fill.  But
        with the enriched payload, a POST-1130-aware consumer can
        place the order directly from this response.
        """
        instrument_key = str(isolated.get("instrument_key") or "").strip()
        matched = branch.get("_matched_level") or {}

        ref_price = branch.get("sr_reference_price")
        try:
            ref_price_f = float(ref_price) if ref_price is not None else None
        except (TypeError, ValueError):
            ref_price_f = None

        stoploss_pct = float(branch.get("stoploss_pct") or 0.0)
        target_points = float(branch.get("target_points") or 0.0)

        pivot_time_iso = branch.get("pivot_time")
        confirmation_time_iso = (
            branch.get("confirmation_time") or matched.get("confirmed_time")
        )

        # ---- 2026-11-05 (D): resolve the actual entry price --------- #
        # The spec's fill price is the close of the confirmation
        # candle — e.g. 113.75 at 13:03 for today's support 99.50
        # trigger.  Resolution order:
        #
        #   1. ``confirmed_time_price`` from the matched level (fastest
        #      path — the enriched dict may carry it if the upstream
        #      producer included it).
        #   2. ``_entry_price_for(key, confirmation_time_iso)`` — the
        #      standard candle-close resolver used by Path A; walks
        #      the SR engine buffer, then the intraday API, then LTP.
        #   3. Reference level price — last-resort fallback.  Wrong as
        #      a fill, but keeps the response self-consistent when the
        #      candle series hasn't been populated yet.
        entry_price: float | None = None
        entry_price_source: str | None = None

        # (1) Prefer the level's confirmed_time_price when it exists.
        try:
            ctp = float(
                matched.get("confirmed_time_price")
                if matched.get("confirmed_time_price") is not None
                else 0.0
            )
        except (TypeError, ValueError):
            ctp = 0.0
        if ctp > 0:
            entry_price = ctp
            entry_price_source = "confirmed_time_price"

        # (2) Standard candle-close resolver.
        if entry_price is None and instrument_key and confirmation_time_iso:
            resolved, src = _entry_price_for(
                instrument_key, str(confirmation_time_iso),
            )
            if resolved is not None and resolved > 0:
                entry_price = float(resolved)
                entry_price_source = src

        # (3) Last-resort reference price fallback.
        if entry_price is None:
            entry_price = ref_price_f
            entry_price_source = "reference_price_fallback"

        # Advisory stop / target — computed from the resolved entry.
        stop_price: float | None = None
        target_price: float | None = None
        if entry_price is not None and entry_price > 0:
            stop_price = round(entry_price * (1.0 - stoploss_pct), 4)
            target_price = round(entry_price + target_points, 4)
        # ------------------------------------------------------------- #

        # Bias side from the isolation record's reason (CE for R3, PE
        # for S3).  The chosen instrument is the ISOLATED instrument
        # itself — the consumer decides direction from
        # ``sr_reference_level`` + ``bias_type``.
        _touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))

        # Reference type as the spec expects it (lowercase "support"
        # / "resistance").  Falls back to the branch's
        # ``sr_reference_level`` when the matched level was enriched
        # but its ``level`` field wasn't normalised.
        reference_type = str(
            branch.get("sr_reference_level")
            or matched.get("level")
            or ""
        ).strip().lower() or None

        candidate = {
            "side": bias_type,
            "instrument_key": instrument_key,
            "trading_symbol": isolated.get("trading_symbol"),
            "is_bias_side": True,
            "state": "armed",
            "state_note": "post_1130_sr_trigger",
            "entry_type": "post_1130_sr_trigger",
            "entry_support": None,
            "blocking_resistance": None,
            "reversal_trigger": None,
            "warning": None,
            "warning_detail": None,
            "entry": {
                # Required spec fields (change D).
                "price": entry_price,
                "price_source": entry_price_source,
                "time": confirmation_time_iso,
                "stop": stop_price,
                "sl_pct": stoploss_pct,
                "stop_source": "post_1130_fixed_pct",
                "target": target_price,
                "target_points": target_points,
                "target_pivot_time": pivot_time_iso,
                # Reference-level context (change D).
                "reference_type": reference_type,
                "reference_price": ref_price_f,
                "pivot_time": pivot_time_iso,
                "confirmation_time": confirmation_time_iso,
            },
        }

        # ---- 2026-11-05 (D): exit_sr_config echoed into post_1130 --- #
        exit_cfg_raw = getattr(
            config, "POST_1130_EXIT_SR_CONFIG", (15, 15)
        )
        try:
            exit_cfg = [int(exit_cfg_raw[0]), int(exit_cfg_raw[1])]
        except (TypeError, ValueError, IndexError):
            exit_cfg = [15, 15]
        # ------------------------------------------------------------- #

        return {
            "status": "ok",
            "execution_path": "POST_1130",
            "strategy_regime": "POST_1130_ISOLATED",
            "day": iso_status.get("day"),
            "isolated": {
                "instrument_key": instrument_key,
                "trading_symbol": isolated.get("trading_symbol"),
                "reason": isolated.get("reason"),
                "source": isolated.get("source"),
                "trigger_index": isolated.get("trigger_index"),
                "routed_index": isolated.get("routed_index"),
                "touched_at": isolated.get("touched_at"),
                "isolated_at": isolated.get("isolated_at"),
                "isolated_time": instrument_data.get("isolated_time"),
                "isolated_touch_time": instrument_data.get("isolated_touch_time"),
                "trigger_is_self_nifty": isolated.get("trigger_is_self_nifty"),
            },
            "post_1130": {
                "regime": "POST_1130_ISOLATED",
                "reference_type": reference_type,
                "reference_price": ref_price_f,
                "pivot_time": pivot_time_iso,
                "confirmation_time": confirmation_time_iso,
                "buffer_minutes": int(branch.get("buffer_minutes") or 0),
                "target_points": target_points,
                "stoploss_pct": stoploss_pct,
                "entry_sr_config": list(
                    instrument_data.get("entry_sr_config") or [15, 30]
                ),
                # 2026-11-05 (D) — the exit tracker reads this.
                "exit_sr_config": exit_cfg,
                "bias_type": bias_type,
                "required_type": branch.get("required_type"),
            },
            "candidates": [candidate],
            "chosen_side": bias_type,
            "chosen_instrument_key": instrument_key,
            "chosen_entry_type": "post_1130_sr_trigger",
            "promotion": {
                "recommended": False,
                "already_applied": False,
                "blockers": ["post_1130_regime_no_promotion"],
                "current_active_instrument_key": instrument_key,
                "target_instrument_key": None,
                "target_trading_symbol": None,
                "target_side": None,
                "official_isolated_key": instrument_key,
                "promotion_allowed": False,
                "re_promotion_allowed": False,
            },
            "active_instrument_key": instrument_key,
            "warnings": [],
            "generated_at": _now_utc_iso(),
        }

    def _build_post_1130_waiting_response(
        self,
        branch: dict[str, Any],
        isolated: dict[str, Any],
        iso_status: dict[str, Any],
        instrument_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Wrap a post-11:30 waiting marker into the standard response shape.

        ``status`` is ``"waiting_post_1130"`` so the admin UI can
        display "waiting" rather than "no_isolation".  The
        ``post_1130.waiting_reason`` field explains WHY we are waiting:

            * ``"no_sr_levels_yet"``                   — no pivots
                                                          detected yet
            * ``"missing_touch_time"``                 — cannot verify
                                                          condition 1
            * ``"no_matching_sr_level"``               — pivots exist
                                                          but none
                                                          satisfy both
                                                          conditions
            * ``"no_matching_sr_level_of_required_type"`` — pivots
                                                          satisfy the
                                                          time
                                                          conditions
                                                          but none are
                                                          of the
                                                          bias-side
                                                          required type
                                                          (support for
                                                          CE,
                                                          resistance
                                                          for PE)
        """
        instrument_key = str(isolated.get("instrument_key") or "").strip()
        reason = str(branch.get("reason") or "unknown")
        _touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))

        warnings: list[str] = []
        if reason == "no_sr_levels_yet":
            warnings.append(
                "POST-1130: waiting for the first (15,30) pivot — "
                "no candles on the bias-side instrument yet."
            )
        elif reason == "missing_touch_time":
            warnings.append(
                "POST-1130: isolated_touch_time is missing from the "
                "isolation record and tracker; cannot verify "
                "condition 1 (pivot_time < touch_time)."
            )
        elif reason == "no_matching_sr_level":
            warnings.append(
                "POST-1130: pivot(s) exist but none satisfy both "
                "conditions (pivot < touch AND confirmation within "
                "the buffer window)."
            )
        elif reason == "no_matching_sr_level_of_required_type":
            req = branch.get("required_type") or "—"
            warnings.append(
                f"POST-1130: pivot(s) satisfy the time conditions but "
                f"none are of the bias-side required type ({req})."
            )

        exit_cfg_raw = getattr(
            config, "POST_1130_EXIT_SR_CONFIG", (15, 15)
        )
        try:
            exit_cfg = [int(exit_cfg_raw[0]), int(exit_cfg_raw[1])]
        except (TypeError, ValueError, IndexError):
            exit_cfg = [15, 15]

        return {
            "status": "waiting_post_1130",
            "execution_path": "POST_1130",
            "strategy_regime": "POST_1130_ISOLATED",
            "day": iso_status.get("day"),
            "isolated": {
                "instrument_key": instrument_key,
                "trading_symbol": isolated.get("trading_symbol"),
                "reason": isolated.get("reason"),
                "source": isolated.get("source"),
                "trigger_index": isolated.get("trigger_index"),
                "routed_index": isolated.get("routed_index"),
                "touched_at": isolated.get("touched_at"),
                "isolated_at": isolated.get("isolated_at"),
                "isolated_time": instrument_data.get("isolated_time"),
                "isolated_touch_time": instrument_data.get("isolated_touch_time"),
                "trigger_is_self_nifty": isolated.get("trigger_is_self_nifty"),
            },
            "post_1130": {
                "regime": "POST_1130_ISOLATED",
                "waiting_reason": reason,
                "buffer_minutes": int(
                    getattr(config, "POST_1130_BUFFER_MINUTES", 10)
                ),
                "target_points": float(
                    getattr(config, "POST_1130_TARGET_POINTS", 50.0)
                ),
                "stoploss_pct": float(
                    getattr(config, "POST_1130_STOPLOSS_PERCENT", 0.30)
                ),
                "entry_sr_config": list(
                    instrument_data.get("entry_sr_config") or [15, 30]
                ),
                "exit_sr_config": exit_cfg,
                "sr_levels_considered": len(
                    instrument_data.get("sr_levels_15_30") or []
                ),
                "bias_type": bias_type,
                "required_type": branch.get("required_type"),
                "time_valid_count": int(branch.get("time_valid_count") or 0),
                "type_filtered_count": int(
                    branch.get("type_filtered_count") or 0
                ),
            },
            "candidates": [],
            "chosen_side": None,
            "chosen_instrument_key": None,
            "chosen_entry_type": None,
            "promotion": {
                "recommended": False,
                "already_applied": False,
                "blockers": ["post_1130_waiting"],
                "current_active_instrument_key": instrument_key,
                "target_instrument_key": None,
                "target_trading_symbol": None,
                "target_side": None,
                "official_isolated_key": instrument_key,
                "promotion_allowed": False,
                "re_promotion_allowed": False,
            },
            "active_instrument_key": instrument_key,
            "warnings": warnings,
            "generated_at": _now_utc_iso(),
        }

    # ------------------------------------------------------------------ #
    # Main entry point                                                   #
    # ------------------------------------------------------------------ #
    def compute(self) -> dict[str, Any]:
        if not getattr(config, "STRATEGY_ENTRY_ENABLED", True):
            return {
                "status": "disabled",
                "reason": "STRATEGY_ENTRY_ENABLED is False",
                "generated_at": _now_utc_iso(),
            }

        try:
            from services.level_isolated_instrument_service import (
                level_isolated_instrument_service,
            )
            iso_status = level_isolated_instrument_service.last_status()
        except Exception as exc:
            logger.exception("[strategy-entry] last_status() raised")
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "generated_at": _now_utc_iso(),
            }

        isolated = (iso_status or {}).get("isolated") or {}
        if not isolated:
            return {
                "status": "no_isolation",
                "note": "No isolated instrument for today.",
                "generated_at": _now_utc_iso(),
            }

        # --------------------------------------------------------------- #
        # 2026-11-05 (third pass) — POST-11:30 regime gate runs FIRST.
        #
        # The POST-1130 regime is keyed off ``isolated_time`` — NOT the
        # trigger source.  A Path B isolation that happens AFTER 11:30
        # therefore routes through POST-1130, superseding Path A/B.
        #
        # This is the fix for the 2026-11-05 backfill replay: the
        # MIDCPNIFTY-triggered 12:59 isolation now correctly produces
        # a POST-1130 CE entry (support 99.50 @ 13:03) instead of a
        # Path B response.
        #
        # ``evaluate_isolated_entry()`` returns a branch marker.  When
        # ``isolated_time.time() >= POST_1130_REGIME_TIME`` (default
        # 11:30 IST) it hands back either a BUY signal or a waiting
        # marker — both are wrapped into responses here and returned
        # WITHOUT running the standard 10-minute scan.
        #
        # When the branch is ``{"_branch": "standard"}`` (or the
        # isolation time is missing / pre-11:30) control falls through
        # to unified standard Path A handling below.
        # --------------------------------------------------------------- #
        instrument_data = self._build_instrument_data(isolated)
        branch = evaluate_isolated_entry(instrument_data)

        if isinstance(branch, dict):
            if branch.get("signal") == "BUY":
                return self._build_post_1130_buy_response(
                    branch, isolated, iso_status, instrument_data,
                )
            if branch.get("_post_1130_waiting"):
                return self._build_post_1130_waiting_response(
                    branch, isolated, iso_status, instrument_data,
                )
            # _branch == "standard" → pre-11:30; fall through.

        # --------------------------------------------------------------- #
        # Unified standard Path A — reuse the former Path B execution
        # mode for EVERY pre-11:30 isolation, regardless of trigger index.
        # The trigger source is retained for diagnostics only.
        # POST-11:30 was evaluated above and remains a separate regime.
        # --------------------------------------------------------------- #
        return self._build_unified_path_a_response(iso_status, isolated)

    def _build_unified_path_a_response(
        self,
        iso_status: dict[str, Any],
        isolated: dict[str, Any],
    ) -> dict[str, Any]:
        """Describe the single standard Path A execution mode.

        This is the former Path B-style standard-session mode: the
        isolation-selected bias instrument stays active and the dummy
        order engine manages post-isolation S/R routing and trade exits.
        It applies equally to NIFTY and external-index isolation triggers.
        POST-11:30 is handled separately before this method is called.
        """
        instrument_key = str(isolated.get("instrument_key") or "").strip()
        underlying = ""
        strike = None
        try:
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.get(instrument_key) if instrument_key else None
            if entry:
                underlying = str(entry.get("underlying") or "").strip()
                strike = entry.get("strike_price")
        except Exception:
            logger.exception(
                "[strategy-entry] unified Path A registry lookup failed for %s",
                instrument_key,
            )

        touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))
        if bias_type is None:
            bias_type = str(isolated.get("type") or "").upper() or None

        return {
            "status": "path_a_unified_mode",
            "execution_path": "PATH_A_UNIFIED",
            "day": iso_status.get("day"),
            "isolated": {
                "instrument_key": instrument_key,
                "trading_symbol": isolated.get("trading_symbol"),
                "underlying": underlying,
                "strike": strike,
                "reason": isolated.get("reason"),
                "source": isolated.get("source"),
                "trigger_index": isolated.get("trigger_index"),
                "routed_index": isolated.get("routed_index"),
                "touched_at": isolated.get("touched_at"),
                "trigger_level": isolated.get("trigger_level") or isolated.get("trigger_side"),
                "trigger_level_value": isolated.get("trigger_level_value"),
                "trigger_is_self_nifty": isolated.get("trigger_is_self_nifty"),
            },
            "path_a": {
                "mode": "unified_standard_sr_execution",
                "trigger_index": isolated.get("trigger_index"),
                "message": (
                    "Unified Path A — all isolation sources use the same "
                    "post-isolation S/R entry and exit engine. The selected "
                    "bias-side instrument remains active."
                ),
                "bias_side": bias_type,
                "bias_instrument_key": instrument_key,
            },
            "candidates": [],
            "chosen_side": None,
            "chosen_instrument_key": None,
            "chosen_entry_type": None,
            "promotion": {
                "recommended": False,
                "already_applied": False,
                "blockers": ["unified_standard_path_no_promotion"],
                "current_active_instrument_key": instrument_key,
                "target_instrument_key": None,
                "target_trading_symbol": None,
                "target_side": None,
                "official_isolated_key": instrument_key,
                "promotion_allowed": False,
                "re_promotion_allowed": False,
            },
            "active_instrument_key": instrument_key,
            "warnings": [
                "Unified Path A standard mode: trigger source does not alter execution."
            ],
            "generated_at": _now_utc_iso(),
        }

    @staticmethod
    def _warnings(
        candidates: list[dict[str, Any]],
        touch_dt: datetime,
        now: datetime,
    ) -> list[str]:
        out: list[str] = []
        for c in candidates:
            side = str(c.get("side") or "")
            state = str(c.get("state") or "")
            if state == "waiting":
                note = c.get("state_note") or ""
                if note == "no_sr_levels":
                    out.append(f"{side}: no fast SR pivots yet.")
                elif note == "no_support_pivot_in_window":
                    out.append(
                        f"{side}: no support pivot inside the "
                        f"post-touch buffer yet."
                    )
            elif state == "waiting_confirmation":
                es = c.get("entry_support") or {}
                br = c.get("blocking_resistance") or {}
                if es.get("price"):
                    out.append(
                        f"{side}: support at {es.get('price')} — "
                        f"waiting for fast confirmation at "
                        f"{es.get('confirmed_time_fast')}"
                    )
                elif br.get("price"):
                    out.append(
                        f"{side}: resistance at {br.get('price')} — "
                        f"waiting for fast confirmation at "
                        f"{br.get('confirmed_time')}"
                    )
            elif state == "blocked":
                note = c.get("state_note") or ""
                if note == "entry_below_support":
                    out.append(
                        f"{side}: entry broke support during confirmation."
                    )
                elif note == "all_candidates_in_window_blocked":
                    out.append(
                        f"{side}: buffer window expired with no "
                        f"tradeable confirmation."
                    )
                elif note == "invalid_counter_price":
                    out.append(
                        f"{side}: auto-reverse failed — counter side "
                        f"price unavailable."
                    )
            elif state == "armed" and c.get("warning"):
                out.append(
                    f"{side}: {c.get('warning')} — {c.get('warning_detail')}"
                )
        _ = now
        return out

strategy_entry_signal_service = StrategyEntrySignalService()