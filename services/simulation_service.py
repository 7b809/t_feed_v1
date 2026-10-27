# services/simulation_service.py
"""Dummy-order replay for the isolated instrument (post-close simulation).

Reads today's isolated-instrument record, the strategy entry signal, and
the option's OWN first-candle OR levels (data/<underlying>/<strike>_<TYPE>/
first_candle.json), then:

  1. Classifies the entry price into a regime based on where it sits
     relative to the option's own OR levels (see the module docstring in
     core/config.py — SIMULATION_* block).
  2. Computes a stop-loss and target for TWO scenarios:
       * signal_v2   — the strategy signal's own target & stop (v2)
       * regime      — the simulation's regime-based target & stop
  3. Replays the day's intraday 1-min candles from the entry fill
     forward, testing each bar's [low, high] against both boundaries.
  4. Returns a side-by-side comparison with the exit reason, exit
     price, PnL% and R-multiple for each scenario.

The service is STATELESS and read-only — no disk writes, no scheduled
jobs, no Telegram. It re-fetches the intraday candle series from Upstox
via ``live_sr_calc_service.fetch_intraday_candles_for_entry`` on every
call, so the replay is always against the authoritative OHLC, not the
tick-aggregated view.

Never raises. All expected failure modes land in the returned ``status``
field (``ok`` / ``disabled`` / ``no_isolation`` / ``no_signal`` /
``no_or_levels`` / ``no_candles`` / ``error``).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
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
    """Parse an ISO-8601 string into a tz-aware datetime (or None)."""
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

# --------------------------------------------------------------------------- #
# Numeric helpers                                                             #
# --------------------------------------------------------------------------- #
def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _get_tick_size(instrument_key: str) -> float:
    """Return the tick size for the instrument (default 0.05 for F&O)."""
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(instrument_key)
        if entry:
            ts = entry.get("tick_size")
            if ts is not None:
                f = _safe_float(ts)
                if f is not None and f > 0:
                    return f
    except Exception:
        pass
    return 0.05

def _round_to_tick(value: float, tick: float, mode: str) -> float:
    """Round ``value`` to the nearest tick according to ``mode``."""
    if tick <= 0 or not math.isfinite(value):
        return float(value)
    mode = (mode or "nearest").lower()
    if mode == "down":
        return math.floor(value / tick) * tick
    if mode == "up":
        return math.ceil(value / tick) * tick
    return round(value / tick) * tick

# --------------------------------------------------------------------------- #
# Regime classification                                                       #
# --------------------------------------------------------------------------- #
def _classify_regime(
    entry_price: float,
    or_levels: dict[str, Any],
    nearest_resistance_above: float | None,
) -> dict[str, Any]:
    """Classify the entry price relative to the option's own OR levels.

    Returns a dict with:
        regime        — LOW_EXTREME / LOW / LOW_MID / MID / HIGH / HIGH_EXTREME
        target_price  — proposed target under this regime
        stop_mode     — "tier" | "range" | "tighter"
        reason        — short human-readable explanation
        levels_used   — the OR levels this decision depended on
    """
    H = _safe_float(or_levels.get("H"))
    L = _safe_float(or_levels.get("L"))
    A = _safe_float(or_levels.get("A"))
    R1 = _safe_float(or_levels.get("R1"))
    R2 = _safe_float(or_levels.get("R2"))
    R3 = _safe_float(or_levels.get("R3"))
    S1 = _safe_float(or_levels.get("S1"))
    S2 = _safe_float(or_levels.get("S2"))
    S3 = _safe_float(or_levels.get("S3"))

    if any(x is None for x in (H, L, A, R1, R2, R3, S1, S2, S3)):
        return {
            "regime": "UNCLASSIFIABLE",
            "target_price": None,
            "stop_mode": "tier",
            "reason": "option's own OR levels are incomplete",
            "levels_used": None,
        }

    # S3 < S2 < S1 < A < R1 < R2 < R3  (by construction)
    step_up = max(0.0, R2 - A)      # range step above the average
    step_down = max(0.0, A - L)     # range step below the average
    stop_mode_high = str(
        getattr(config, "SIMULATION_STOP_MODE_HIGH", "tighter")
    ).lower()
    if stop_mode_high not in {"tier", "range", "tighter"}:
        stop_mode_high = "tighter"

    if entry_price < S3:
        return {
            "regime": "LOW_EXTREME",
            "target_price": L,
            "stop_mode": "tier",
            "reason": (
                f"entry {entry_price:.2f} < S3 {S3:.2f} — deep discount "
                f"vs OR; bounce to OR low L={L:.2f}"
            ),
            "levels_used": {"S3": S3, "L": L},
        }

    if entry_price < S2:
        return {
            "regime": "LOW",
            "target_price": L,
            "stop_mode": "tier",
            "reason": (
                f"entry {entry_price:.2f} in S3..S2 band "
                f"({S3:.2f}..{S2:.2f}); bounce to OR low L={L:.2f}"
            ),
            "levels_used": {"S3": S3, "S2": S2, "L": L},
        }

    if entry_price < A:
        return {
            "regime": "LOW_MID",
            "target_price": A,
            "stop_mode": "tier",
            "reason": (
                f"entry {entry_price:.2f} in S2..A band; "
                f"mean-revert to OR average A={A:.2f}"
            ),
            "levels_used": {"S2": S2, "A": A},
        }

    if entry_price < R2:
        target = nearest_resistance_above if nearest_resistance_above is not None else A
        return {
            "regime": "MID",
            "target_price": target,
            "stop_mode": "tier",
            "reason": (
                f"entry {entry_price:.2f} in A..R2 band; nearest "
                f"resistance above = {target:.2f}"
            ),
            "levels_used": {"A": A, "R2": R2, "nearest_resistance_above": nearest_resistance_above},
        }

    if entry_price < R3:
        return {
            "regime": "HIGH",
            "target_price": entry_price + step_up,
            "stop_mode": stop_mode_high,
            "reason": (
                f"entry {entry_price:.2f} in R2..R3 band "
                f"({R2:.2f}..{R3:.2f}); high-vol continuation — "
                f"project +{step_up:.2f} above entry "
                f"(step = R2 − A = {R2 - A:.2f}); "
                f"stop={stop_mode_high}(tier, A−L={step_down:.2f})"
            ),
            "levels_used": {
                "R2": R2, "R3": R3, "A": A, "L": L,
                "step_up": step_up, "step_down": step_down,
            },
        }

    # entry >= R3
    return {
        "regime": "HIGH_EXTREME",
        "target_price": entry_price + step_up,
        "stop_mode": stop_mode_high,
        "reason": (
            f"entry {entry_price:.2f} ≥ R3 {R3:.2f}; high-vol "
            f"continuation — project +{step_up:.2f} above entry "
            f"(step = R2 − A = {R2 - A:.2f}); "
            f"stop={stop_mode_high}(tier, A−L={step_down:.2f})"
        ),
        "levels_used": {
            "R3": R3, "R2": R2, "A": A, "L": L,
            "step_up": step_up, "step_down": step_down,
        },
    }

# --------------------------------------------------------------------------- #
# Nearest resistance above (for the MID regime)                              #
# --------------------------------------------------------------------------- #
def _nearest_resistance_above(
    entry_price: float,
    pivots: list[dict[str, Any]],
) -> float | None:
    """Return the smallest resistance pivot price strictly above entry."""
    best: float | None = None
    for p in pivots:
        if str(p.get("level") or "").lower() != "resistance":
            continue
        try:
            price = float(p["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= entry_price:
            continue
        if best is None or price < best:
            best = price
    return best

# --------------------------------------------------------------------------- #
# Stop-loss resolution                                                        #
# --------------------------------------------------------------------------- #
def _resolve_stop(
    entry_price: float,
    stop_mode: str,
    tier_stop: float,
    or_levels: dict[str, Any],
    instrument_key: str,
) -> tuple[float, str]:
    """Return (stop_price, stop_source) for a scenario."""
    A = _safe_float(or_levels.get("A"))
    L = _safe_float(or_levels.get("L"))

    tick = _get_tick_size(instrument_key)
    rounding = str(getattr(config, "SIMULATION_TICK_ROUNDING", "nearest")).lower()

    mode = str(stop_mode or "tier").lower()

    range_stop: float | None = None
    if A is not None and L is not None and (A - L) > 0:
        range_stop = entry_price - (A - L)

    if mode == "range" and range_stop is not None:
        stop = range_stop
        source = "range"
    elif mode == "tighter" and range_stop is not None:
        stop = min(tier_stop, range_stop)
        source = "min(tier,range)"
    else:
        stop = tier_stop
        source = "tier"

    rounded = _round_to_tick(stop, tick, rounding)
    if rounded <= 0:
        rounded = stop
    return float(rounded), source

# --------------------------------------------------------------------------- #
# Replay                                                                      #
# --------------------------------------------------------------------------- #
def _replay_scenario(
    candles: list[dict[str, Any]],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    target_price: float | None,
    instrument_key: str,
) -> dict[str, Any]:
    """Replay the intraday candles from entry forward; return exit info.

    Skips candles whose timestamp is ≤ ``entry_time_iso`` (the entry fill
    happens at the close of the confirming bar; the next bar starts
    after that moment).
    """
    entry_dt = _parse_iso(entry_time_iso)
    if entry_dt is None:
        return {
            "status": "invalid_entry_time",
            "error": f"cannot parse entry_time={entry_time_iso!r}",
        }

    same_bar_stop_first = bool(
        getattr(config, "SIMULATION_SAME_BAR_STOP_FIRST", True)
    )

    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    candles_scanned = 0
    max_high = entry_price
    min_low = entry_price
    mfe = 0.0            # max favorable excursion (up only)
    mae = 0.0            # max adverse excursion (down only, negative)

    for c in candles:
        c_dt = _parse_iso(c.get("time"))
        if c_dt is None:
            continue
        if c_dt <= entry_dt:
            continue
        candles_scanned += 1

        hi = _safe_float(c.get("high"))
        lo = _safe_float(c.get("low"))
        if hi is None or lo is None:
            continue

        max_high = max(max_high, hi)
        min_low = min(min_low, lo)
        mfe = max(mfe, hi - entry_price)
        mae = min(mae, lo - entry_price)

        hit_stop = lo <= stop_price
        hit_target = (target_price is not None) and (hi >= target_price)

        if hit_stop and hit_target:
            if same_bar_stop_first:
                exit_price = stop_price
                exit_reason = "STOP_HIT_SAME_BAR_CONSERVATIVE"
            else:
                exit_price = target_price
                exit_reason = "TARGET_HIT_SAME_BAR_OPTIMISTIC"
            exit_time = str(c.get("time"))
            break

        if hit_stop:
            exit_price = stop_price
            exit_time = str(c.get("time"))
            exit_reason = "STOP_HIT"
            break

        if hit_target:
            exit_price = target_price
            exit_time = str(c.get("time"))
            exit_reason = "TARGET_HIT"
            break

    # ---- Unresolved — use last available close ---- #
    if exit_price is None:
        last_close: float | None = None
        last_time: str | None = None
        for c in reversed(candles):
            c_dt = _parse_iso(c.get("time"))
            if c_dt is None or c_dt <= entry_dt:
                continue
            cl = _safe_float(c.get("close"))
            if cl is not None:
                last_close = cl
                last_time = str(c.get("time"))
                break
        if last_close is None:
            exit_price = entry_price
            exit_reason = "UNRESOLVED_NO_DATA"
        else:
            exit_price = last_close
            exit_time = last_time
            exit_reason = "UNRESOLVED_END_OF_SESSION"

    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    stop_dist_pct = (
        (entry_price - stop_price) / entry_price if entry_price > 0 else 0.0
    )
    r_multiple = (
        pnl_pct / stop_dist_pct if stop_dist_pct > 0 else None
    )

    return {
        "status": "replayed",
        "candles_scanned": candles_scanned,
        "exit_price": round(exit_price, 4),
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct, 6),
        "r_multiple": round(r_multiple, 4) if r_multiple is not None else None,
        "max_high": round(max_high, 4),
        "min_low": round(min_low, 4),
        "mfe": round(mfe, 4),
        "mae": round(mae, 4),
    }

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class SimulationService:
    """Dummy-order replay for the currently isolated instrument."""

    def run(self) -> dict[str, Any]:
        if not bool(getattr(config, "SIMULATION_ENABLED", True)):
            return {
                "status": "disabled",
                "reason": "SIMULATION_ENABLED is False",
                "generated_at": _now_utc_iso(),
            }

        # ---- 1. Read the entry signal ---------------------------- #
        try:
            from services.strategy_entry_signal_service import (
                strategy_entry_signal_service,
            )
            signal = strategy_entry_signal_service.compute()
        except Exception as exc:
            logger.exception("[simulation] entry-signal compute raised")
            return {
                "status": "error",
                "error": f"entry_signal: {type(exc).__name__}: {exc}",
                "generated_at": _now_utc_iso(),
            }

        if not isinstance(signal, dict) or signal.get("status") != "ok":
            return {
                "status": "no_signal",
                "reason": (signal or {}).get("status") or "no_signal",
                "detail": (signal or {}).get("note") or (signal or {}).get("error"),
                "generated_at": _now_utc_iso(),
            }

        # ---- 2. Find the chosen (armed) candidate ---------------- #
        chosen = None
        for c in signal.get("candidates") or []:
            if c.get("chosen"):
                chosen = c
                break
        if chosen is None or chosen.get("state") != "armed":
            return {
                "status": "no_signal",
                "reason": "no_candidate_armed",
                "chosen_side": signal.get("chosen_side"),
                "candidates_state": [
                    {"side": c.get("side"), "state": c.get("state")}
                    for c in signal.get("candidates") or []
                ],
                "generated_at": _now_utc_iso(),
            }

        entry = chosen.get("entry") or {}
        entry_price = _safe_float(entry.get("price"))
        entry_time = entry.get("time")
        signal_stop = _safe_float(entry.get("stop"))
        signal_target = _safe_float(entry.get("target"))

        if entry_price is None or not entry_time or signal_stop is None:
            return {
                "status": "no_signal",
                "reason": "incomplete_entry_block",
                "chosen_side": chosen.get("side"),
                "generated_at": _now_utc_iso(),
            }

        instrument_key = str(chosen.get("instrument_key") or "").strip()
        if not instrument_key:
            return {
                "status": "no_signal",
                "reason": "missing_instrument_key",
                "generated_at": _now_utc_iso(),
            }

        # ---- 3. Read the option's own OR levels ------------------ #
        or_levels = self._read_option_or_levels(
            instrument_key, chosen.get("side")
        )
        if or_levels is None:
            return {
                "status": "no_or_levels",
                "reason": "option's own first_candle.json missing",
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        # ---- 4. Fetch the intraday candle series ----------------- #
        candles = self._fetch_intraday_candles(instrument_key)
        if not candles:
            return {
                "status": "no_candles",
                "reason": "intraday API returned no candles",
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        # ---- 5. Fast pivot scan (for the nearest resistance) ----- #
        try:
            from services.strategy_entry_signal_service import _run_pivot_scan
            left_bars = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS",
                config.LIVE_SR_LEFT_BARS,
            ))
            right_bars = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3,
            ))
            pivots = _run_pivot_scan(instrument_key, left_bars, right_bars)
        except Exception:
            pivots = []

        nearest_r_above = _nearest_resistance_above(entry_price, pivots)

        # ---- 6. Regime classification ---------------------------- #
        regime = _classify_regime(entry_price, or_levels, nearest_r_above)

        # ---- 7. Build scenarios --------------------------------- #
        tick = _get_tick_size(instrument_key)
        rounding = str(getattr(
            config, "SIMULATION_TICK_ROUNDING", "nearest",
        )).lower()

        # Tier stop (base of both scenarios)
        tier_pct = _safe_float(entry.get("sl_pct")) or 0.15
        tier_stop = entry_price * (1.0 - tier_pct)

        # ---- Scenario A: v2 signal ---- #
        sig_stop_price = _round_to_tick(signal_stop, tick, rounding)
        if sig_stop_price <= 0:
            sig_stop_price = signal_stop
        sig_target_price = (
            _round_to_tick(signal_target, tick, rounding)
            if signal_target is not None else None
        )

        scenario_signal = self._build_scenario(
            label="Signal (v2 — nearest resistance)",
            entry_price=entry_price,
            stop_price=sig_stop_price,
            target_price=sig_target_price,
            stop_source="tier",
            candles=candles,
            entry_time_iso=entry_time,
            instrument_key=instrument_key,
        )

        scenarios: dict[str, Any] = {"signal_v2": scenario_signal}

        # ---- Scenario B: regime ---- #
        if bool(getattr(config, "SIMULATION_USE_REGIME_TARGETS", True)):
            regime_stop, regime_stop_source = _resolve_stop(
                entry_price=entry_price,
                stop_mode=regime.get("stop_mode", "tier"),
                tier_stop=tier_stop,
                or_levels=or_levels,
                instrument_key=instrument_key,
            )
            regime_target = regime.get("target_price")
            if regime_target is not None:
                regime_target = _round_to_tick(regime_target, tick, rounding)

            scenario_regime = self._build_scenario(
                label=f"Regime (v2 — {regime.get('regime')})",
                entry_price=entry_price,
                stop_price=regime_stop,
                target_price=regime_target,
                stop_source=regime_stop_source,
                candles=candles,
                entry_time_iso=entry_time,
                instrument_key=instrument_key,
            )
            scenarios["regime"] = scenario_regime

        # ---- 8. Verdict ------------------------------------------ #
        verdict = self._verdict(scenarios)

        # ---- 9. Assemble response -------------------------------- #
        return {
            "status": "ok",
            "day": _now_market().date().isoformat(),
            "generated_at": _now_utc_iso(),
            "instrument_key": instrument_key,
            "trading_symbol": chosen.get("trading_symbol"),
            "side": chosen.get("side"),
            "entry": {
                "price": entry_price,
                "time": entry_time,
                "price_source": entry.get("price_source"),
                "sl_pct": tier_pct,
                "tier_stop": round(tier_stop, 4),
            },
            "or_levels": or_levels,
            "regime": regime,
            "scenarios": scenarios,
            "verdict": verdict,
            "notes": self._notes(regime, scenarios, or_levels, entry_price),
            "candles_total": len(candles),
        }

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_scenario(
        *,
        label: str,
        entry_price: float,
        stop_price: float,
        target_price: float | None,
        stop_source: str,
        candles: list[dict[str, Any]],
        entry_time_iso: str,
        instrument_key: str,
    ) -> dict[str, Any]:
        stop_pct = (
            (entry_price - stop_price) / entry_price if entry_price > 0 else None
        )
        target_pct = (
            (target_price - entry_price) / entry_price
            if (target_price is not None and entry_price > 0) else None
        )
        rr_ratio = (
            (target_pct / stop_pct) if (
                target_pct is not None
                and stop_pct is not None
                and stop_pct > 0
            ) else None
        )

        replay = _replay_scenario(
            candles=candles,
            entry_time_iso=entry_time_iso,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            instrument_key=instrument_key,
        )

        return {
            "label": label,
            "entry_price": round(entry_price, 4),
            "stop_price": round(stop_price, 4),
            "target_price": round(target_price, 4) if target_price is not None else None,
            "stop_source": stop_source,
            "stop_pct": round(stop_pct, 6) if stop_pct is not None else None,
            "target_pct": round(target_pct, 6) if target_pct is not None else None,
            "rr_ratio": round(rr_ratio, 4) if rr_ratio is not None else None,
            "replay": replay,
        }

    @staticmethod
    def _verdict(scenarios: dict[str, Any]) -> dict[str, Any]:
        """Compare the two scenarios and pick a winner (if any)."""
        sig = scenarios.get("signal_v2") or {}
        reg = scenarios.get("regime") or {}

        sig_r = ((sig.get("replay") or {}).get("r_multiple"))
        reg_r = ((reg.get("replay") or {}).get("r_multiple"))

        if sig_r is None and reg_r is None:
            return {"winner": "unknown", "reason": "no_r_multiple"}

        if sig_r is None:
            return {"winner": "regime", "reason": "signal_missing_r"}
        if reg_r is None:
            return {"winner": "signal_v2", "reason": "regime_missing_r"}

        if abs(sig_r - reg_r) < 0.01:
            return {"winner": "tie", "reason": "r_multiple_equal"}
        if reg_r > sig_r:
            return {
                "winner": "regime",
                "reason": f"regime R={reg_r:.2f} > signal R={sig_r:.2f}",
                "r_delta": round(reg_r - sig_r, 4),
            }
        return {
            "winner": "signal_v2",
            "reason": f"signal R={sig_r:.2f} > regime R={reg_r:.2f}",
            "r_delta": round(sig_r - reg_r, 4),
        }

    @staticmethod
    def _notes(
        regime: dict[str, Any],
        scenarios: dict[str, Any],
        or_levels: dict[str, Any],
        entry_price: float,
    ) -> list[str]:
        notes: list[str] = []
        notes.append(f"Regime: {regime.get('regime')} — {regime.get('reason')}")

        sig = scenarios.get("signal_v2") or {}
        reg = scenarios.get("regime") or {}

        sig_r = (sig.get("replay") or {}).get("r_multiple")
        reg_r = (reg.get("replay") or {}).get("r_multiple")

        if sig.get("target_price") is not None and reg.get("target_price") is not None:
            if abs(sig["target_price"] - reg["target_price"]) > 0.5:
                notes.append(
                    f"Targets differ: signal={sig['target_price']} vs "
                    f"regime={reg['target_price']} "
                    f"(Δ={reg['target_price'] - sig['target_price']:+.2f})"
                )

        if sig.get("stop_price") is not None and reg.get("stop_price") is not None:
            if abs(sig["stop_price"] - reg["stop_price"]) > 0.05:
                notes.append(
                    f"Stops differ: signal={sig['stop_price']} vs "
                    f"regime={reg['stop_price']} "
                    f"(Δ={reg['stop_price'] - sig['stop_price']:+.2f})"
                )

        if sig_r is not None and reg_r is not None:
            notes.append(
                f"R-multiple: signal={sig_r:+.2f}R, regime={reg_r:+.2f}R"
            )

        # Sanity check: entry below support (would be a v2 hard-block)
        try:
            entry_support_price = None
            from services.strategy_entry_signal_service import (
                strategy_entry_signal_service as _svc,
            )
            sig_resp = _svc.compute()
            for c in (sig_resp.get("candidates") or []):
                if c.get("chosen"):
                    es = c.get("entry_support") or {}
                    entry_support_price = _safe_float(es.get("price"))
                    break
            if entry_support_price is not None and entry_price < entry_support_price:
                notes.append(
                    f"⚠️  entry {entry_price:.2f} is BELOW the entry support "
                    f"{entry_support_price:.2f} — v2 would have blocked this "
                    f"trade as 'entry_below_support'."
                )
        except Exception:
            pass

        return notes

    @staticmethod
    def _read_option_or_levels(
        instrument_key: str,
        side: str | None,
    ) -> dict[str, Any] | None:
        """Read the option's own first_candle.json → or_levels dict."""
        import json
        from pathlib import Path
        try:
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.get(instrument_key)
        except Exception:
            entry = None
        if entry is None:
            return None

        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or side or "").upper()
        if not underlying or strike is None or not strike_type:
            return None
        try:
            strike_int = int(float(strike))
        except (TypeError, ValueError):
            return None

        path = (
            Path(config.LIVE_DATA_DIR)
            / underlying
            / f"{strike_int}_{strike_type}"
            / "first_candle.json"
        )
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        or_levels = payload.get("or_levels")
        if not isinstance(or_levels, dict):
            return None
        return or_levels

    @staticmethod
    def _fetch_intraday_candles(instrument_key: str) -> list[dict[str, Any]]:
        """Fetch today's 1-min intraday candles via the shared calc loader."""
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            return live_sr_calc_service.fetch_intraday_candles_for_entry(
                instrument_key
            )
        except Exception:
            logger.exception(
                "[simulation] intraday candle fetch failed for %s",
                instrument_key,
            )
            return []


simulation_service = SimulationService()