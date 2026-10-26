# services/strategy_entry_signal_service.py
"""R3/S3-touch -> buffered support pivot -> confirmed entry signal.

Reads today's isolation record (from `level_isolated_instrument_service`)
and the persisted SR levels of both the bias side and the counter side
(from `live_sr_levels_service`) and produces the entry-signal block that
powers the "Entry Signal" card on `/isolated`.

Signal chain (see core/config.py STRATEGY_ENTRY_* block for tunables):

    touch_time  ─┐
                 │   window = [touch_time, touch_time + BUFFER]
                 ▼
    ┌───────────────────────────┐
    │  SUPPORT pivot in window  │   for each candidate side
    └────────────┬──────────────┘
                 │
                 ▼
    ┌───────────────────────────┐
    │  Not blocked by a         │   resistance pivot
    │  resistance?              │   pivot_time > support.pivot_time
    │                           │   price     > support.price
    │                           │   confirmed ≤ support.confirmed_time
    └────────────┬──────────────┘
                 │
                 ▼
    ┌───────────────────────────┐
    │  Wait for confirmed_time  │   ARMED on confirmed_time
    │  → entry LTP = candle     │   (candle close at confirmed_time,
    │    close at confirmed     │    falls back to live LTP)
    └───────────────────────────┘

Crossover
---------
When `STRATEGY_ENTRY_ALLOW_CROSSOVER` is True, BOTH the bias side and
the counter side are evaluated every poll. Whichever reaches ARMED with
the earliest `confirmed_time` becomes the chosen candidate; the other
(if it also arms) is marked "discarded". When False, only the bias side
is evaluated (no crossover).

Promotion
---------
When the CHOSEN side differs from the official isolated instrument
(bias side), the response includes a `promotion` block that tells the
caller to promote the counter-side instrument to the active/official
pick.  Applying the promotion is delegated to
`isolated_instrument_promotion_service` — this service stays a pure
read/compute routine (no side effects).  The promotion block also
exposes `active_instrument_key`, which is the promoted key if a
promotion has already been applied today, else the original isolated
key.

Once-per-day
------------
`STRATEGY_ENTRY_ONLY_ONCE_PER_DAY` (default True) means that once a
candidate side is ARMED and shown, that side is not re-armed even if a
new support confirms later. `chosen_locked` is the flag the UI uses to
display the final pick.

Threading
---------
All state is recomputed on every `compute()` call. There is no mutable
state on the service itself (unlike the SR tracker), so multiple
concurrent readers are safe. The service holds no locks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _iso(dt: datetime) -> str:
    return dt.astimezone(_market_tz()).isoformat()


def _minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


# --------------------------------------------------------------------------- #
# Touch-side parsing                                                          #
# --------------------------------------------------------------------------- #
def _parse_touch_reason(reason: Any) -> tuple[str | None, str | None]:
    """Return ``(touch_side, bias_type)`` from an isolation reason string.

    Recognised substrings (case-insensitive):
      * ``r3`` -> touch_side="R3", bias_type="CE"
      * ``s3`` -> touch_side="S3", bias_type="PE"
    """
    r = str(reason or "").lower()
    if "r3" in r:
        return "R3", "CE"
    if "s3" in r:
        return "S3", "PE"
    return None, None


# --------------------------------------------------------------------------- #
# Candle-close-at-time lookup                                                 #
# --------------------------------------------------------------------------- #
def _candle_close_at(instrument_key: str, iso_time: str) -> float | None:
    """Return the close of the 1-min candle that ended at ``iso_time``.

    Prefers the live SR engine's in-memory candle buffer (which is seeded
    from the merged historical + intraday series by ``sr_backfill_service``
    on every refresh, so it holds the full session's candles for backfill
    and live mode both).

    Falls back to ``None`` on any failure — the caller then falls back to
    the live LTP.
    """
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
    """Return ``(price, source)`` for the entry.

    Source is one of:
      * ``"candle_close_at_confirmed"`` — the exact LTP at confirmation
      * ``"live_ltp"``                  — fallback to current live LTP
      * ``None``                        — no price available
    """
    close = _candle_close_at(instrument_key, confirmed_time_iso)
    if close is not None:
        return close, "candle_close_at_confirmed"
    ltp = _live_ltp(instrument_key)
    if ltp is not None:
        return ltp, "live_ltp"
    return None, None


# --------------------------------------------------------------------------- #
# SR levels reading                                                           #
# --------------------------------------------------------------------------- #
def _read_levels(instrument_key: str) -> list[dict[str, Any]]:
    """Return today's persisted SR levels for ``instrument_key`` (or [])."""
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        payload = live_sr_levels_service.get_levels(instrument_key)
    except Exception:
        logger.exception(
            "[strategy-entry] live_sr_levels_service lookup failed for %s",
            instrument_key,
        )
        return []

    if not isinstance(payload, dict):
        return []

    levels = payload.get("levels") or []
    out: list[dict[str, Any]] = []
    for lvl in levels:
        if isinstance(lvl, dict):
            out.append(dict(lvl))
    return out


# --------------------------------------------------------------------------- #
# Counterpart lookup                                                          #
# --------------------------------------------------------------------------- #
def _both_sides(
    underlying: str,
    strike: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(ce_entry, pe_entry)`` at the given (underlying, strike)."""
    try:
        from services.instrument_registry import instrument_registry
    except Exception:
        return None, None

    ce = instrument_registry.resolve(underlying, strike, "CE")
    pe = instrument_registry.resolve(underlying, strike, "PE")
    return ce, pe


# --------------------------------------------------------------------------- #
# Per-candidate signal                                                        #
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
        "state": "waiting",  # waiting | waiting_confirmation | blocked | armed | discarded
        "state_note": None,
        "entry_support": None,
        "blocking_resistance": None,
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
) -> dict[str, Any]:
    """Compute the signal for ONE candidate side (CE or PE)."""
    if entry is None:
        return _empty_candidate(side, None, None, is_bias_side)

    key = str(entry.get("instrument_key") or "").strip()
    symbol = entry.get("trading_symbol")
    cand = _empty_candidate(side, key, symbol, is_bias_side)

    levels = _read_levels(key)
    if not levels:
        cand["state_note"] = "no_sr_levels"
        return cand

    # ---- Collect supports in the [touch_time, window_end] window --- #
    supports_in_window: list[dict[str, Any]] = []
    for lvl in levels:
        if str(lvl.get("level") or "").lower() != "support":
            continue
        p_dt = _parse_iso(lvl.get("pivot_time"))
        if p_dt is None:
            continue
        if p_dt < touch_dt:
            continue
        if p_dt > window_end:
            continue
        try:
            price = float(lvl.get("price"))
        except (TypeError, ValueError):
            continue
        supports_in_window.append({
            "price": price,
            "pivot_time": _iso(p_dt),
            "confirmed_time": lvl.get("confirmed_time"),
            "pivot_dt": p_dt,
            "raw": lvl,
        })

    if not supports_in_window:
        cand["state"] = "waiting"
        cand["state_note"] = "no_support_pivot_in_window"
        return cand

    # ---- Earliest pivot first ------------------------------------ #
    supports_in_window.sort(key=lambda s: s["pivot_dt"])

    # ---- Iterate — first non-blocked support wins ---------------- #
    for s in supports_in_window:
        support_pivot_dt = s["pivot_dt"]
        support_confirmed_dt = _parse_iso(s["confirmed_time"])
        if support_confirmed_dt is None:
            # Malformed confirmed_time — skip.
            continue

        # ---- Blocking resistance check -------------------------- #
        blocker: dict[str, Any] | None = None
        if config.STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE:
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
                if r_conf_dt > support_confirmed_dt:
                    continue
                # First blocking resistance wins.
                if blocker is None or r_price < float(blocker.get("price") or 0.0):
                    blocker = {
                        "price": r_price,
                        "pivot_time": _iso(r_pivot_dt),
                        "confirmed_time": lvl.get("confirmed_time"),
                    }

        if blocker is not None:
            # This support is blocked — try the next one in the window.
            cand["blocking_resistance"] = blocker
            cand["state_note"] = "all_supports_blocked_so_far"
            continue

        # ---- Not blocked — check confirmation ------------------ #
        minutes_from_touch = _minutes_between(touch_dt, support_pivot_dt)
        entry_support = {
            "price": s["price"],
            "pivot_time": s["pivot_time"],
            "confirmed_time": s["confirmed_time"],
            "minutes_from_touch": round(minutes_from_touch, 2),
        }
        cand["entry_support"] = entry_support
        cand["blocking_resistance"] = None

        if support_confirmed_dt > now:
            cand["state"] = "waiting_confirmation"
            cand["state_note"] = "support_found_waiting_for_confirmation"
            cand["seconds_until_confirmation"] = int(
                (support_confirmed_dt - now).total_seconds()
            )
            return cand

        # ---- ARMED --------------------------------------------- #
        entry_price, price_source = _entry_price_for(
            key, s["confirmed_time"],
        )
        stop_price = float(s["price"])

        # Soft stop-width warning.
        warning = None
        warning_detail = None
        stop_pct: float | None = None
        if entry_price is not None and entry_price > 0:
            stop_pct = (entry_price - stop_price) / entry_price
            if stop_pct < 0:
                warning = "price_at_or_below_support"
                warning_detail = (
                    "Entry LTP is at or below the entry support price — "
                    "the support did not hold during the confirmation wait."
                )
            elif (
                config.STRATEGY_MAX_STOP_PCT > 0
                and stop_pct > config.STRATEGY_MAX_STOP_PCT
            ):
                warning = "stop_too_wide"
                warning_detail = (
                    f"Stop {stop_price:.2f} is {stop_pct * 100:.1f}% below "
                    f"entry {entry_price:.2f} "
                    f"(max {config.STRATEGY_MAX_STOP_PCT * 100:.1f}%)."
                )

        # Target = next resistance above entry, or None.
        target_price: float | None = None
        target_pivot_time: str | None = None
        if entry_price is not None:
            best_r: dict[str, Any] | None = None
            for lvl in levels:
                if str(lvl.get("level") or "").lower() != "resistance":
                    continue
                try:
                    r_price = float(lvl.get("price"))
                except (TypeError, ValueError):
                    continue
                if r_price <= entry_price:
                    continue
                if best_r is None or r_price < float(best_r.get("price") or 0.0):
                    best_r = lvl
            if best_r is not None:
                try:
                    target_price = float(best_r.get("price"))
                except (TypeError, ValueError):
                    target_price = None
                target_pivot_time = best_r.get("pivot_time")

        cand["state"] = "armed"
        cand["state_note"] = "entry_support_confirmed"
        cand["warning"] = warning
        cand["warning_detail"] = warning_detail
        cand["entry"] = {
            "price": entry_price,
            "price_source": price_source,
            "time": s["confirmed_time"],
            "stop": stop_price,
            "stop_pct": round(stop_pct, 4) if stop_pct is not None else None,
            "target": target_price,
            "target_pivot_time": target_pivot_time,
        }
        return cand

    # Every support in the window was blocked.
    cand["state"] = "blocked"
    cand["state_note"] = "all_supports_in_window_blocked"
    return cand


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class StrategyEntrySignalService:
    """Stateless computation of the entry-signal block for /isolated."""

    def compute(self) -> dict[str, Any]:
        # ---- 0. Feature toggle ---------------------------------- #
        if not config.STRATEGY_ENTRY_ENABLED:
            return {
                "status": "disabled",
                "reason": "STRATEGY_ENTRY_ENABLED is False",
                "generated_at": _now_utc_iso(),
            }

        # ---- 1. Read the isolation record ----------------------- #
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

        instrument_key = str(isolated.get("instrument_key") or "").strip()
        if not instrument_key:
            return {
                "status": "no_isolation",
                "note": "Isolation record has no instrument_key.",
                "generated_at": _now_utc_iso(),
            }

        # ---- 2. Touch time -------------------------------------- #
        touch_time_iso = (
            isolated.get("touched_at")
            or isolated.get("isolated_at")
            or isolated.get("trigger", {}).get("touched_at")
        )
        touch_dt = _parse_iso(touch_time_iso)
        if touch_dt is None:
            return {
                "status": "no_touch_time",
                "note": (
                    "Isolation record has no touched_at / isolated_at, "
                    "cannot anchor the strategy window."
                ),
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        # ---- 3. Side / bias ------------------------------------- #
        touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))
        if touch_side is None or bias_type is None:
            return {
                "status": "no_touch_side",
                "note": (
                    "Isolation reason does not encode R3 or S3; "
                    "cannot determine the bias side."
                ),
                "instrument_key": instrument_key,
                "reason": isolated.get("reason"),
                "generated_at": _now_utc_iso(),
            }

        # ---- 4. Resolve the isolated registry entry + both sides - #
        try:
            from services.instrument_registry import instrument_registry
            iso_entry = instrument_registry.get(instrument_key)
        except Exception:
            iso_entry = None

        if iso_entry is None:
            return {
                "status": "no_registry_entry",
                "note": (
                    "The isolated instrument is not in the registry — "
                    "cannot resolve strike / underlying."
                ),
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        underlying = str(iso_entry.get("underlying") or "").strip()
        strike = iso_entry.get("strike_price")

        if not underlying or strike is None:
            return {
                "status": "incomplete_registry_entry",
                "note": "Registry entry missing underlying / strike_price.",
                "instrument_key": instrument_key,
                "underlying": underlying or None,
                "strike": strike,
                "generated_at": _now_utc_iso(),
            }

        ce_entry, pe_entry = _both_sides(underlying, strike)

        if bias_type == "CE":
            bias_entry = ce_entry
            counter_entry = pe_entry
        else:  # bias_type == "PE"
            bias_entry = pe_entry
            counter_entry = ce_entry

        # ---- 5. Build candidates -------------------------------- #
        now = _now_market()
        window_end = touch_dt + timedelta(
            minutes=int(config.STRATEGY_ENTRY_BUFFER_MINUTES)
        )

        candidate_specs: list[tuple[str, dict[str, Any] | None, bool]] = []

        if bias_entry is not None:
            candidate_specs.append((bias_type, bias_entry, True))

        if config.STRATEGY_ENTRY_ALLOW_CROSSOVER and counter_entry is not None:
            opposite = "PE" if bias_type == "CE" else "CE"
            candidate_specs.append((opposite, counter_entry, False))

        candidates: list[dict[str, Any]] = []
        for side, entry, is_bias in candidate_specs:
            try:
                cand = _compute_candidate(
                    side=side,
                    entry=entry,
                    is_bias_side=is_bias,
                    touch_dt=touch_dt,
                    window_end=window_end,
                    now=now,
                )
            except Exception as exc:
                logger.exception(
                    "[strategy-entry] candidate compute failed for side=%s",
                    side,
                )
                cand = _empty_candidate(side, None, None, is_bias)
                cand["state"] = "error"
                cand["state_note"] = f"{type(exc).__name__}: {exc}"
            candidates.append(cand)

        # ---- 6. First-to-confirm winner ------------------------- #
        chosen: dict[str, Any] | None = None
        armed = [c for c in candidates if c.get("state") == "armed"]

        if armed:
            if config.STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS and len(armed) > 1:
                def _confirmed_dt(c: dict[str, Any]) -> str:
                    es = c.get("entry_support") or {}
                    return str(es.get("confirmed_time") or "")
                armed.sort(key=_confirmed_dt)
                chosen = armed[0]
                for c in armed[1:]:
                    c["state"] = "discarded"
                    c["state_note"] = "first_to_confirm_won_by_other_side"
            else:
                chosen = armed[0]

        # Mark chosen boolean on every candidate for easy UI use.
        for c in candidates:
            c["chosen"] = (chosen is not None and c is chosen)

        # ---- 7. Assemble response ------------------------------- #
        response: dict[str, Any] = {
            "status": "ok",
            "day": iso_status.get("day"),
            "isolated": {
                "instrument_key": instrument_key,
                "trading_symbol": isolated.get("trading_symbol"),
                "underlying": underlying,
                "strike": strike,
                "reason": isolated.get("reason"),
                "touched_at": touch_time_iso,
                # Trigger metadata — the promotion service needs these
                # to build the promoted record with the original touch
                # context preserved (level, value, index).
                "trigger_level": (
                    isolated.get("trigger_level")
                    or isolated.get("trigger_side")
                ),
                "trigger_level_value": isolated.get("trigger_level_value"),
                "trigger_index": isolated.get("trigger_index"),
            },
            "touch": {
                "time": _iso(touch_dt),
                "side": touch_side,
                "bias_type": bias_type,
                "buffer_minutes": int(config.STRATEGY_ENTRY_BUFFER_MINUTES),
                "window_start": _iso(touch_dt),
                "window_end": _iso(window_end),
            },
            "candidates": candidates,
            "chosen_side": chosen.get("side") if chosen else None,
            "chosen_instrument_key": (
                chosen.get("instrument_key") if chosen else None
            ),
            "warnings": self._warnings(candidates, touch_dt, now),
            "generated_at": _now_utc_iso(),
        }

        # ---- 8. Promotion decision block ------------------------ #
        # Pure read-only check.  Actual promotion is applied by
        # `isolated_instrument_promotion_service.apply_promotion(...)`.
        try:
            from services.isolated_instrument_promotion_service import (
                isolated_instrument_promotion_service,
            )
            active_state = isolated_instrument_promotion_service.read_active()
        except Exception:
            active_state = None

        chosen_key = response.get("chosen_instrument_key")
        active_key = (
            active_state.get("to_instrument_key")
            if active_state else None
        )

        already_applied = (
            active_key is not None
            and chosen_key is not None
            and str(active_key) == str(chosen_key)
        )

        blockers: list[str] = []
        if not config.STRATEGY_ENTRY_ALLOW_PROMOTION:
            blockers.append("disabled")
        if chosen_key is None:
            blockers.append("no_armed_candidate")
        elif chosen_key == instrument_key:
            blockers.append("chosen_is_bias_side")
        elif already_applied:
            blockers.append("already_applied")
        elif (
            active_state is not None
            and not config.STRATEGY_ENTRY_ALLOW_RE_PROMOTION
        ):
            blockers.append("already_promoted_today")

        response["promotion"] = {
            "recommended": len(blockers) == 0,
            "already_applied": already_applied,
            "blockers": blockers,
            "current_active_instrument_key": active_key,
            "target_instrument_key": chosen_key,
            "target_trading_symbol": (
                chosen.get("trading_symbol") if chosen else None
            ),
            "target_side": response.get("chosen_side"),
            "official_isolated_key": instrument_key,
            "promotion_allowed": bool(config.STRATEGY_ENTRY_ALLOW_PROMOTION),
            "re_promotion_allowed": bool(
                config.STRATEGY_ENTRY_ALLOW_RE_PROMOTION
            ),
        }
        response["active_instrument_key"] = active_key or instrument_key

        return response

    # ------------------------------------------------------------------ #
    # Warnings                                                           #
    # ------------------------------------------------------------------ #
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
                    out.append(
                        f"{side}: no live SR levels persisted yet for this instrument"
                    )
                elif note == "no_support_pivot_in_window":
                    out.append(
                        f"{side}: no support pivot inside "
                        f"[{_iso(touch_dt)} → +"
                        f"{config.STRATEGY_ENTRY_BUFFER_MINUTES}min]"
                    )
            elif state == "blocked":
                blocker = c.get("blocking_resistance") or {}
                out.append(
                    f"{side}: blocked by resistance "
                    f"{blocker.get('price')} "
                    f"(confirmed {blocker.get('confirmed_time')})"
                )
            elif state == "armed" and c.get("warning"):
                out.append(
                    f"{side}: {c.get('warning')} — {c.get('warning_detail')}"
                )

        if not any(c.get("state") == "armed" for c in candidates):
            # Nothing armed yet — informational only.
            if all(c.get("state") == "waiting" for c in candidates):
                out.append(
                    "No candidate has produced a support pivot inside the "
                    "post-touch buffer yet."
                )

        _ = now  # reserved for future timing warnings
        return out


# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
strategy_entry_signal_service = StrategyEntrySignalService()