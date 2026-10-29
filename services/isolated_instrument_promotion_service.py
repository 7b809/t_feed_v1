# services/isolated_instrument_promotion_service.py
"""Promote the entry-signal winner to be the active isolated instrument.

Background
----------
The isolation engine (``level_isolated_instrument_service``) fires on
the first R3 / S3 touch and permanently binds itself to the **bias
side** instrument (CE for R3, PE for S3) for the day. It cannot be re-run.

The strategy-entry engine (``strategy_entry_signal_service``) evaluates
BOTH sides at the isolated strike (crossover). Whichever side's post-
touch support confirms first becomes ``chosen_instrument_key``.

When the chosen instrument is the **counter side** (e.g. PE bias, CE
wins), the two disagree: the official isolated instrument is PE, but
the signal says CE is the actual trade. This service resolves the
disagreement by recording a **promotion** — the counter-side
instrument becomes the "active" one that the SR tracker, hero card,
and snapshot card focus on. The isolation record itself is never
modified (it is the historical truth of what triggered at T_touch);
the original instrument is preserved inside the promotion sidecar
under ``record`` and the trigger_event stays intact in the isolation
record.

G1 v4.1 — entry freeze (2026-10-29)
-----------------------------------
The previous revision captured the entry block only inside
``record`` (as ``instrument_ltp``) and ``evidence`` (as
``entry_ltp`` / ``entry_stop`` / ``entry_target``). The
``/isolated`` page then re-ran ``strategy_entry_signal_service.compute()``
every 10 s to render the entry-signal card, which recomputed the
entry/stop/target on every poll. After the ``_entry_price_for`` fix
in ``strategy_entry_signal_service.py`` the recompute now returns the
correct **candle close at ``confirmed_time_fast``** — but re-computing
it on every poll still risks drift if the underlying candle state
changes (cache purges, intraday API hiccups, next-day restart, etc.)
and guarantees the entry-signal card can disagree with the
dummy-order service (which creates its record once and never
recomputes).

To make the entry block fully stable for the whole day, the promotion
now snapshots the chosen candidate **once** into a new
``frozen_entry`` block at the top level of the sidecar. Every
subsequent poll reads that frozen block instead of re-computing.

The sidecar's ``frozen_entry`` shape::

    {
      "captured_at":          "2026-10-29T09:23:01+05:30",
      "chosen_instrument_key": "NSE_FO|53793",
      "chosen_trading_symbol": "NIFTY 22750 PE 03 NOV 26",
      "chosen_side":          "PE",
      "chosen_candidate": {
        "instrument_key":    "NSE_FO|53793",
        "trading_symbol":    "NIFTY 22750 PE 03 NOV 26",
        "side":              "PE",
        "is_bias_side":      false,
        "state":             "armed",
        "state_note":        "auto_reverse_counter_side",
        "entry_type":        "auto_reverse_on_rejection",
        "entry_support":     null,
        "reversal_trigger":  {
          "bias_resistance_price":              162.0,
          "bias_resistance_pivot_time":         "2026-10-29T09:20:00+05:30",
          "bias_resistance_confirmed_time_fast": "2026-10-29T09:23:00+05:30",
          "rejected_side":                      "CE"
        },
        "entry": {
          "price":             186.55,
          "price_source":      "candle_close_at_confirmed_fast",
          "time":              "2026-10-29T09:23:00+05:30",
          "stop":              158.57,
          "stop_pct":          0.15,
          "sl_pct":            0.15,
          "stop_source":       "ltp_tier",
          "target":            110.65,
          "target_pivot_time": "2026-10-29T10:53:00+05:30"
        },
        "warning":           null,
        "warning_detail":    null,
        "chosen":            true
      },
      "rejected_candidate": {
        "instrument_key":  "NSE_FO|53792",
        "trading_symbol":  "NIFTY 22750 CE 03 NOV 26",
        "side":            "CE",
        "state":           "discarded",
        "state_note":      "rejected_and_reversed",
        "chosen":          false,
        ... (same field shape as chosen_candidate)
      },
      "signal_generated_at":  "2026-10-29T09:23:01+05:30",
      "signal_day":           "2026-10-29",
      "touch": {
        "time":           "2026-10-29T09:18:00+05:30",
        "side":           "R3",
        "bias_type":      "CE",
        "buffer_minutes": 10,
        "window_start":   "2026-10-29T09:18:00+05:30",
        "window_end":     "2026-10-29T09:28:00+05:30"
      }
    }

Sidecar file (unchanged path)
-----------------------------
``<LIVE_DATA_DIR>/level_isolated_instrument_promotion.json``

Public API
----------
    isolated_instrument_promotion_service.read_active() -> dict | None
    isolated_instrument_promotion_service.is_promoted_today() -> bool
    isolated_instrument_promotion_service.get_frozen_entry() -> dict | None
    isolated_instrument_promotion_service.refresh_frozen_entry(signal) -> dict
    isolated_instrument_promotion_service.apply_promotion(signal) -> dict
    isolated_instrument_promotion_service.clear() -> dict
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _today_iso() -> str:
    return datetime.now(_MARKET_TZ).date().isoformat()

def _now_market_iso() -> str:
    return datetime.now(_MARKET_TZ).isoformat()

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _sidecar_path() -> Path:
    return Path(config.LIVE_DATA_DIR) / "level_isolated_instrument_promotion.json"

def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MARKET_TZ)
    return dt

def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _read_first_candle_json(
    underlying: str,
    strike: Any,
    side: str,
) -> dict[str, Any]:
    """Read the option's ``first_candle.json`` from disk (best-effort)."""
    try:
        strike_int = int(float(strike))
    except (TypeError, ValueError):
        return {}
    path = (
        Path(config.LIVE_DATA_DIR)
        / str(underlying).strip().lower()
        / f"{strike_int}_{str(side).strip().upper()}"
        / "first_candle.json"
    )
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

def _confirmation_bar_ohlc(
    instrument_key: str,
    confirmed_time_iso: str,
) -> dict[str, Any]:
    """Return OHLC of the 1-min candle that ended at ``confirmed_time_iso``."""
    try:
        from services.live_sr_levels_service import live_sr_levels_service
    except Exception:
        return {}

    try:
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
    except Exception:
        return {}

    if not state:
        return {}

    target = _parse_iso_datetime(confirmed_time_iso)
    if target is None:
        return {}
    target_minute = target.replace(second=0, microsecond=0)

    for c in state.get("candles") or []:
        c_dt = _parse_iso_datetime(c.get("time"))
        if c_dt is None:
            continue
        if c_dt.replace(second=0, microsecond=0) == target_minute:
            return {
                "time_iso": c.get("time"),
                "open": _safe_float(c.get("open")),
                "high": _safe_float(c.get("high")),
                "low": _safe_float(c.get("low")),
                "close": _safe_float(c.get("close")),
                "volume": c.get("volume"),
            }
    return {}

def _live_ltp(instrument_key: str) -> tuple[float | None, str]:
    try:
        from services.live_feed_service import live_feed_service
        ltp = live_feed_service.get_latest_ltp(instrument_key)
        if ltp is not None:
            return float(ltp), "live_feed_cache"
    except Exception:
        pass
    return None, "unavailable"

# --------------------------------------------------------------------------- #
# NEW: frozen-entry extraction                                               #
# --------------------------------------------------------------------------- #
def _extract_candidate_block(c: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, UI-facing fields of a single candidate block.

    This is what gets frozen at promotion time so the entry-signal
    card cannot drift between polls. Only the fields the template
    actually renders are kept, so the sidecar stays small.
    """
    if not isinstance(c, dict):
        return {}
    return {
        "instrument_key": c.get("instrument_key"),
        "trading_symbol": c.get("trading_symbol"),
        "side": c.get("side"),
        "is_bias_side": bool(c.get("is_bias_side")),
        "state": c.get("state"),
        "state_note": c.get("state_note"),
        "entry_type": c.get("entry_type"),
        "entry_support": c.get("entry_support"),
        "reversal_trigger": c.get("reversal_trigger"),
        "entry": c.get("entry"),
        "warning": c.get("warning"),
        "warning_detail": c.get("warning_detail"),
        "chosen": bool(c.get("chosen")),
    }

def _build_frozen_entry(
    signal: dict[str, Any],
    target_cand: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``frozen_entry`` block for the promotion sidecar.

    Called exactly once per promotion. Subsequent polls read the
    frozen block instead of re-running ``strategy_entry_signal_service
    .compute()``, so the entry / stop / target / warnings shown on
    ``/isolated`` cannot drift.

    The frozen block captures:

      * the **chosen** candidate (the armed one), with the full
        ``entry``, ``entry_support``, ``reversal_trigger``, and
        ``warning`` sub-blocks;
      * the **rejected** candidate (the discarded bias-side one),
        for the "Bias CE rejected at ₹162.00 (confirmed 09:23:00)"
        line in the UI;
      * the touch metadata (time, side, bias type, window bounds) so
        the entry-signal header strip stays stable too;
      * the ``signal_generated_at`` timestamp so the operator can see
        when the freeze was captured.
    """
    candidates = signal.get("candidates") or []
    chosen_side = str(target_cand.get("side") or "").upper()

    chosen_block = _extract_candidate_block(target_cand)

    # Find the "rejected" candidate (the first non-chosen candidate
    # whose side differs from the chosen one). This is the bias-side
    # discarded entry when auto-reverse fired, or the "other side"
    # placeholder candidate when the bias side armed directly.
    rejected_block: dict[str, Any] | None = None
    for c in candidates:
        if not isinstance(c, dict):
            continue
        if c is target_cand:
            continue
        c_side = str(c.get("side") or "").upper()
        if c_side == chosen_side:
            continue
        rejected_block = _extract_candidate_block(c)
        break

    return {
        "captured_at": _now_market_iso(),
        "chosen_instrument_key": target_cand.get("instrument_key"),
        "chosen_trading_symbol": target_cand.get("trading_symbol"),
        "chosen_side": chosen_side,
        "chosen_candidate": chosen_block,
        "rejected_candidate": rejected_block,
        "signal_generated_at": signal.get("generated_at"),
        "signal_day": signal.get("day"),
        "touch": signal.get("touch") or {},
    }

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class IsolatedInstrumentPromotionService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # State accessors                                                     #
    # ------------------------------------------------------------------ #
    def read_active(self) -> dict[str, Any] | None:
        """Return today's promotion record, or None."""
        path = _sidecar_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[promotion] unreadable sidecar file")
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("day") or "") != _today_iso():
            return None
        return payload

    def is_promoted_today(self) -> bool:
        return self.read_active() is not None

    # ------------------------------------------------------------------ #
    # NEW: frozen-entry accessors                                         #
    # ------------------------------------------------------------------ #
    def get_frozen_entry(self) -> dict[str, Any] | None:
        """Return today's frozen entry block, or ``None``.

        The frozen entry is written by ``apply_promotion()`` at the
        moment the promotion is first applied. It captures the chosen
        candidate's entry / stop / target / warnings so the
        ``/isolated`` page cannot drift between polls.

        Returns ``None`` when:

          * no promotion has been applied today, OR
          * the sidecar exists but predates the ``frozen_entry``
            feature (legacy record — see ``refresh_frozen_entry()``).
        """
        active = self.read_active()
        if not isinstance(active, dict):
            return None
        frozen = active.get("frozen_entry")
        if isinstance(frozen, dict):
            return frozen
        return None

    def refresh_frozen_entry(
        self,
        signal: dict[str, Any],
    ) -> dict[str, Any]:
        """Recompute and rewrite the frozen entry block.

        Intended for two recovery scenarios only:

          1. **Legacy records** — a promotion sidecar written before
             the ``frozen_entry`` feature existed. Rewrites the
             sidecar with a fresh ``frozen_entry`` built from the
             current ``signal``.
          2. **Manual recovery** — the operator suspects the frozen
             values are wrong (e.g. after fixing an upstream bug) and
             wants to re-capture from the current signal.

        Never runs automatically. ``apply_promotion()`` calls the
        same backfill internally for the legacy case; this method
        is the manual escape hatch.

        Never raises. Returns ``{"status": "ok", "frozen_entry": ...}``
        on success, or ``{"status": "error", "error": ...}`` on failure.
        """
        if not isinstance(signal, dict) or signal.get("status") != "ok":
            return {
                "status": "error",
                "error": "no_valid_signal",
                "generated_at": _now_utc_iso(),
            }

        with self._lock:
            active = self.read_active()
            if not isinstance(active, dict):
                return {
                    "status": "no_active_promotion",
                    "generated_at": _now_utc_iso(),
                }

            target_key = str(active.get("to_instrument_key") or "").strip()
            if not target_key:
                return {
                    "status": "error",
                    "error": "sidecar_missing_target_key",
                    "generated_at": _now_utc_iso(),
                }

            target_cand: dict[str, Any] | None = None
            for c in signal.get("candidates") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("instrument_key") or "") == target_key:
                    target_cand = c
                    break
            if target_cand is None:
                return {
                    "status": "error",
                    "error": "target_not_in_candidates",
                    "target_instrument_key": target_key,
                    "generated_at": _now_utc_iso(),
                }

            frozen = _build_frozen_entry(signal, target_cand)
            active["frozen_entry"] = frozen
            # Also backfill ``record`` fields that the template reads,
            # so legacy records become fully consistent with the fresh
            # signal (only when the record has no value yet).
            record = active.get("record")
            if isinstance(record, dict):
                if record.get("instrument_ltp") in (None, "") and frozen["chosen_candidate"].get("entry"):
                    record["instrument_ltp"] = frozen["chosen_candidate"]["entry"].get("price")
                    record["instrument_quote_source"] = frozen["chosen_candidate"]["entry"].get("price_source")

            try:
                path = _sidecar_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(active, indent=2, default=str),
                    encoding="utf-8",
                )
                tmp.replace(path)
            except Exception as exc:
                logger.exception(
                    "[promotion] refresh_frozen_entry write failed"
                )
                return {
                    "status": "error",
                    "error": f"write_failed: {type(exc).__name__}",
                    "generated_at": _now_utc_iso(),
                }

            logger.info(
                "[promotion] frozen_entry refreshed for %s "
                "(chosen_side=%s)",
                target_key,
                frozen.get("chosen_side"),
            )
            return {
                "status": "ok",
                "frozen_entry": frozen,
                "generated_at": _now_utc_iso(),
            }

    # ------------------------------------------------------------------ #
    # Apply promotion                                                     #
    # ------------------------------------------------------------------ #
    def apply_promotion(self, signal: dict[str, Any]) -> dict[str, Any]:
        # G1 v4.0 — prefer the new STRATEGY_AUTO_PROMOTION_ENABLED flag,
        # which is aliased in core/config.py to fall back to the v3.2
        # STRATEGY_ENTRY_ALLOW_PROMOTION when the new env var is unset.
        if not getattr(
            config, "STRATEGY_AUTO_PROMOTION_ENABLED",
            getattr(config, "STRATEGY_ENTRY_ALLOW_PROMOTION", True),
        ):
            return {
                "promoted": False,
                "reason": "promotion_disabled",
                "generated_at": _now_utc_iso(),
            }

        if not isinstance(signal, dict) or signal.get("status") != "ok":
            return {
                "promoted": False,
                "reason": "no_valid_signal",
                "generated_at": _now_utc_iso(),
            }

        promotion = signal.get("promotion") or {}
        if not promotion.get("recommended"):
            return {
                "promoted": False,
                "reason": "not_recommended",
                "generated_at": _now_utc_iso(),
            }

        target_key = str(promotion.get("target_instrument_key") or "").strip()
        if not target_key:
            return {
                "promoted": False,
                "reason": "no_target",
                "generated_at": _now_utc_iso(),
            }

        # Look up the target candidate early so both the idempotent
        # branch (for legacy backfill) and the new-promotion branch
        # can use it.
        target_cand: dict[str, Any] | None = None
        for c in signal.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            if str(c.get("instrument_key") or "") == target_key:
                target_cand = c
                break
        if target_cand is None:
            return {
                "promoted": False,
                "reason": "target_not_in_candidates",
                "generated_at": _now_utc_iso(),
            }

        # Defensive: allow_re_promotion may be missing from a
        # mis-configured .env; default to False (v3.2 behaviour).
        allow_re_promotion = bool(getattr(
            config, "STRATEGY_ENTRY_ALLOW_RE_PROMOTION", False,
        ))

        with self._lock:
            existing = self.read_active()
            if existing is not None:
                existing_key = str(existing.get("to_instrument_key") or "")
                if existing_key == target_key:
                    # ------------------------------------------------ #
                    # Idempotent re-promote.                          #
                    # If the existing sidecar was written before the  #
                    # frozen_entry feature, backfill it now so the   #
                    # UI stabilises.                                  #
                    # ------------------------------------------------ #
                    if not existing.get("frozen_entry"):
                        try:
                            frozen = _build_frozen_entry(signal, target_cand)
                            existing["frozen_entry"] = frozen
                            path = _sidecar_path()
                            path.parent.mkdir(parents=True, exist_ok=True)
                            tmp = path.with_suffix(path.suffix + ".tmp")
                            tmp.write_text(
                                json.dumps(existing, indent=2, default=str),
                                encoding="utf-8",
                            )
                            tmp.replace(path)
                            logger.info(
                                "[promotion] backfilled frozen_entry for "
                                "legacy promotion record — key=%s "
                                "chosen_side=%s entry_price=%s source=%s",
                                target_key,
                                frozen.get("chosen_side"),
                                (frozen.get("chosen_candidate") or {})
                                .get("entry", {})
                                .get("price"),
                                (frozen.get("chosen_candidate") or {})
                                .get("entry", {})
                                .get("price_source"),
                            )
                        except Exception:
                            logger.exception(
                                "[promotion] frozen_entry backfill failed "
                                "for legacy record — keeping sidecar as-is"
                            )

                    return {
                        "promoted": False,
                        "reason": "already_promoted",
                        "existing": existing,
                        "generated_at": _now_utc_iso(),
                    }
                if not allow_re_promotion:
                    return {
                        "promoted": False,
                        "reason": "already_promoted_other_target",
                        "existing": existing,
                        "generated_at": _now_utc_iso(),
                    }

            isolated_block = signal.get("isolated") or {}
            underlying = str(isolated_block.get("underlying") or "").strip()
            strike = isolated_block.get("strike")
            if not underlying or strike is None:
                return {
                    "promoted": False,
                    "reason": "missing_underlying_or_strike",
                    "generated_at": _now_utc_iso(),
                }

            side = str(target_cand.get("side") or "").strip().upper()
            if side not in {"CE", "PE"}:
                return {
                    "promoted": False,
                    "reason": "invalid_side",
                    "generated_at": _now_utc_iso(),
                }

            # Registry entry (for trading_symbol / expiry / levels)
            try:
                from services.instrument_registry import instrument_registry
                reg_entry = instrument_registry.get(target_key) or {}
            except Exception:
                reg_entry = {}

            trading_symbol = (
                target_cand.get("trading_symbol")
                or reg_entry.get("trading_symbol")
                or target_key
            )
            expiry = (
                target_cand.get("expiry")
                or reg_entry.get("expiry")
            )

            # ---- Capture the promoted instrument's snapshot -------- #
            ltp, ltp_source = _live_ltp(target_key)
            first_candle_payload = _read_first_candle_json(
                underlying, strike, side,
            )
            fc_obj = first_candle_payload.get("first_candle") or {}
            or_levels = first_candle_payload.get("or_levels") or {}

            entry_support = target_cand.get("entry_support") or {}
            entry = target_cand.get("entry") or {}
            confirmed_time_iso = str(entry_support.get("confirmed_time") or "")
            touch_ohlc = (
                _confirmation_bar_ohlc(target_key, confirmed_time_iso)
                if confirmed_time_iso else {}
            )

            # LTP fallback chain
            quote_source = ltp_source
            if ltp is None and entry.get("price") is not None:
                ltp = _safe_float(entry.get("price"))
                quote_source = "entry_ltp_fallback"
            if ltp is None and touch_ohlc.get("close") is not None:
                ltp = _safe_float(touch_ohlc.get("close"))
                quote_source = "confirmation_bar_close"

            trigger_info = signal.get("touch") or {}

            record = {
                "instrument_key": target_key,
                "trading_symbol": trading_symbol,
                "underlying": underlying,
                "strike": strike,
                "type": side,
                "expiry": expiry,
                "reason": isolated_block.get("reason"),
                "trigger_level": (
                    isolated_block.get("trigger_level")
                    or trigger_info.get("side")
                ),
                "trigger_level_value": isolated_block.get("trigger_level_value"),
                "trigger_index": (
                    isolated_block.get("trigger_index")
                    or trigger_info.get("bias_type")
                ),
                "isolated_at": _now_market_iso(),
                "touched_at": (
                    trigger_info.get("time")
                    or isolated_block.get("touched_at")
                ),
                "instrument_ltp": ltp,
                "instrument_quote_source": quote_source,
                "instrument_touch_ohlc": touch_ohlc,
                "instrument_first_candle": fc_obj,
                "instrument_or_levels": or_levels,
                "levels": (
                    target_cand.get("levels")
                    or reg_entry.get("levels")
                    or []
                ),
                "promoted_at": _now_market_iso(),
                "promoted_from_key": (
                    (existing or {}).get("to_instrument_key")
                    or isolated_block.get("instrument_key")
                ),
                "promoted_from_symbol": (
                    (existing or {}).get("to_trading_symbol")
                    or isolated_block.get("trading_symbol")
                ),
            }

            # ---- Freeze the entry block ---------------------------- #
            # Captured exactly once per promotion. Consumed by the
            # /isolated page so the entry-signal card cannot drift
            # between polls. On a re-promotion (allow_re_promotion),
            # this overwrites the previous frozen block with the
            # fresh one derived from the new signal.
            frozen_entry = _build_frozen_entry(signal, target_cand)

            payload = {
                "day": _today_iso(),
                "promoted_at": _now_market_iso(),
                "from_instrument_key": record["promoted_from_key"],
                "from_trading_symbol": record["promoted_from_symbol"],
                "to_instrument_key": target_key,
                "to_trading_symbol": trading_symbol,
                "reason": "strategy_entry_armed",
                "evidence": {
                    "entry_support_price": entry_support.get("price"),
                    "entry_support_pivot_time": entry_support.get("pivot_time"),
                    "entry_support_confirmed_time": confirmed_time_iso,
                    "entry_ltp": entry.get("price"),
                    "entry_price_source": entry.get("price_source"),
                    "entry_stop": entry.get("stop"),
                    "entry_target": entry.get("target"),
                    "strategy_chosen_side": signal.get("chosen_side"),
                },
                "frozen_entry": frozen_entry,
                "record": record,
            }

            # ---- Persist sidecar atomically ------------------------ #
            try:
                path = _sidecar_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(payload, indent=2, default=str),
                    encoding="utf-8",
                )
                tmp.replace(path)
            except Exception as exc:
                logger.exception("[promotion] sidecar write failed")
                return {
                    "promoted": False,
                    "reason": f"write_failed: {type(exc).__name__}",
                    "generated_at": _now_utc_iso(),
                }

            # ---- Switch the SR tracker to the promoted instrument -- #
            try:
                from services.isolated_sr_tracker_service import (
                    isolated_sr_tracker_service,
                )
                isolated_sr_tracker_service.on_isolation(record)
            except Exception:
                logger.exception("[promotion] SR tracker switch failed")

            # ---- Telegram (informational) ------------------------- #
            try:
                from services.telegram_service import telegram_service
                msg = (
                    f"{_DIV}\n"
                    f"🎯 INSTRUMENT PROMOTED\n"
                    f"{_DIV}\n"
                    f"📌 From:        "
                    f"{payload['from_trading_symbol']} "
                    f"({payload['from_instrument_key']})\n"
                    f"📌 To:          "
                    f"{payload['to_trading_symbol']} "
                    f"({payload['to_instrument_key']})\n"
                    f"🎯 Reason:      strategy entry ARMED on "
                    f"{signal.get('chosen_side')} side\n"
                    f"🕒 Triggered:   {trigger_info.get('side')} @ "
                    f"{trigger_info.get('time')}\n"
                    f"🕒 Promoted:    {payload['promoted_at']}\n"
                    f"{_THIN}\n"
                    f"💰 Entry LTP:   {entry.get('price')} "
                    f"({entry.get('price_source')})\n"
                    f"🛑 Stop:        {entry.get('stop')}\n"
                    f"🎯 Target:      {entry.get('target')}\n"
                    f"{_THIN}\n"
                    f"📊 SR tracker is now tracking the promoted instrument.\n"
                    f"💾 Saved to data/level_isolated_instrument_promotion.json"
                )
                telegram_service.send_message(
                    "Instrument Promoted", msg, level="SUCCESS",
                )
            except Exception:
                logger.exception("[promotion] Telegram send failed")

            logger.info(
                "[promotion] PROMOTED %s -> %s (reason=%s, entry_ltp=%s, "
                "entry_source=%s, frozen_entry=true)",
                payload["from_instrument_key"],
                payload["to_instrument_key"],
                payload["reason"],
                entry.get("price"),
                entry.get("price_source"),
            )

            return {
                "promoted": True,
                "promotion": payload,
                "generated_at": _now_utc_iso(),
            }

    # ------------------------------------------------------------------ #
    # Clear                                                               #
    # ------------------------------------------------------------------ #
    def clear(self) -> dict[str, Any]:
        with self._lock:
            path = _sidecar_path()
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                logger.exception("[promotion] sidecar delete failed")
                return {
                    "status": "error",
                    "error": "delete_failed",
                    "generated_at": _now_utc_iso(),
                }
            try:
                from services.isolated_sr_tracker_service import (
                    isolated_sr_tracker_service,
                )
                isolated_sr_tracker_service.reset()
            except Exception:
                logger.exception("[promotion] SR tracker reset failed")
            logger.info("[promotion] state cleared")
            return {"status": "ok", "generated_at": _now_utc_iso()}

isolated_instrument_promotion_service = IsolatedInstrumentPromotionService()