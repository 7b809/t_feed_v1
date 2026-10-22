# File: services/strategy_2_order_service.py
"""STRATEGY 2 — Stage 4: next-cross order placement.

Inputs:
    * The isolated contract chosen by Stage 3
      (data/strategy2/stage_3_isolation_level/stage_3_summary.json)
    * That contract's live EMA engine candles (via live_ema_engine)

Logic:
    1. Read the isolated contract and its S3-touch minute.
    2. Scan that contract's EMA candles for the FIRST fast/slow cross
       whose timestamp is STRICTLY AFTER the S3-touch minute.
    3. On that cross:
         bullish  -> BUY  the same contract  (e.g. CE stays CE)
         bearish  -> BUY  the OPPOSITE type  (e.g. CE -> PE @ same strike)
       Use the cross price for the same-type order, or the sibling
       contract's own LTP at that minute for the opposite-type order
       (best-effort; falls back to the cross price when unavailable).

Paper order only — the real Upstox order API is not wired.
The audit payload records: touch minute, cross minute, direction,
chosen instrument, entry price, and the reason.

Diagnostics (added for the 2026-10-22 observability patch set)
--------------------------------------------------------------
Before this revision the module used ``logging.getLogger(__name__)``
and therefore wrote nothing to ``logs/strategy_2_order_service.log``
or ``logs_text/strategy_2_order_service.txt`` — every log line from
this file was console-only. Combined with three genuinely different
failure states being collapsed into a single ``no_isolated`` message,
this made the S2 Stage-4 pipeline effectively un-diagnosable from the
log stream alone.

This module now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * classifies the Stage-3 summary into four explicit states via
      ``_classify_stage3_summary()``:

          "missing"         — stage_3_summary.json does not exist
          "no_isolated"     — exists, but ``isolated`` is null
          "no_touch_minute" — exists, ``isolated`` is present, but
                              it has no ``touch_minute`` field
          "ok"              — exists, ``isolated`` is present with a
                              touch minute

      and emits a tailored message per state (INFO for the normal
      "waiting for a touch" case, WARNING for the two failure states);

    * reports candle stats when no cross is found: total candles in
      the engine buffer, how many fell at or before the touch minute,
      how many after, and the last candle minute — so an operator can
      distinguish "engine has no data yet" from "engine has data but
      no cross fired";

    * logs one INFO line immediately before placing the order, naming
      the cross minute, type, close, and both EMA values;

    * emits a WARNING when a bearish cross needs the opposite-type
      contract and ``_resolve_opposite_key()`` returned null (the
      sibling was never resolved in Stage 1, so Stage 4 cannot place
      an opposite-type paper order on it);

    * emits a WARNING when ``entry_ltp`` ends up null after a cross
      was detected, naming the source that failed (``cross_close``
      or ``sibling_ltp_at_cross_minute``);

    * adds ``stage_3_summary_state`` / ``sibling_resolved`` /
      ``entry_ltp_available`` / ``candle_stats`` to the returned dict
      and to ``stage_4_summary.json`` for every status.

Signature note
--------------
``_ema_series`` is imported from ``services.live_ema_engine`` because
S2 Stage 4 must use the *same* EMA math as the live engine — this
guarantees the cross detected here matches what the engine itself
would have emitted. Do not swap this for a local re-implementation.
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.live_ema_engine import _ema_series  # reuse EMA math

logger = get_logger(__file__)

_MARKET_CLOSE_TIME = dtime(15, 30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _market_now() -> datetime:
    return datetime.now(_market_tz())


def _market_hhmmss() -> str:
    return _market_now().strftime("%H:%M:%S")


def _post_market() -> bool:
    return _market_now().time() > _MARKET_CLOSE_TIME


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _safe(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


def _stage3_root() -> Path:
    return Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_3_NAME


def _stage4_root() -> Path:
    return Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_4_NAME


def _flip_type(strike_type: str) -> str:
    st = str(strike_type or "").upper()
    if st == "CE":
        return "PE"
    if st == "PE":
        return "CE"
    return st


def _classify_stage3_summary(stage3: dict[str, Any], exists: bool) -> str:
    """Return a short state string for the Stage-3 summary payload.

    Values:
        "missing"          — the file does not exist. Stage 3 never ran,
                             or its write failed.
        "no_isolated"      — file exists but ``isolated`` is null. This
                             is the normal "no contract has touched its
                             S3 level yet" state.
        "no_touch_minute"  — ``isolated`` exists but carries no
                             ``touch_minute``. Malformed record —
                             usually a manual edit or an older schema.
        "ok"               — ``isolated`` is present with a touch
                             minute; proceed to cross detection.
    """
    if not exists:
        return "missing"
    if not stage3:
        return "no_isolated"
    isolated = stage3.get("isolated")
    if not isolated:
        return "no_isolated"
    if not str(isolated.get("touch_minute") or "").strip():
        return "no_touch_minute"
    return "ok"


# ---------------------------------------------------------------------------
# Cross detection on a candle series
# ---------------------------------------------------------------------------
def _find_first_cross_after(
    candles: list[dict[str, Any]],
    *,
    since_minute_prefix: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return (cross_record_or_None, scan_stats).

    The ``scan_stats`` dict explains *why* a ``None`` was returned —
    how many candles were seen, how many fell at or before the touch
    minute, how many after, and the last candle minute in the buffer.
    This is surfaced by the caller's ``no_cross_after_touch`` INFO
    line so an operator can tell "engine has no data yet" apart from
    "engine has data but no cross fired".

    The cross is the first fast/slow EMA cross whose candle minute is
    STRICTLY greater than ``since_minute_prefix``.
    """
    stats: dict[str, Any] = {
        "total_candles": len(candles),
        "usable_candles": 0,
        "before_or_at_touch": 0,
        "after_touch": 0,
        "last_candle_minute": None,
        "ema_computable": False,
        "ema_required": max(config.EMA_SLOW_PERIOD + 2, 3),
    }

    if not candles:
        return None, stats

    valid = [c for c in candles if c.get("close") is not None]
    stats["usable_candles"] = len(valid)
    stats["last_candle_minute"] = (
        str(valid[-1].get("minute")) if valid else None
    )
    if not valid:
        return None, stats

    if len(valid) < stats["ema_required"]:
        return None, stats

    stats["ema_computable"] = True

    # Count how many usable candles fall on each side of the touch
    # minute so a "no cross" outcome can be diagnosed without
    # re-running the scan.
    for c in valid:
        minute = str(c.get("minute") or "")
        if not minute:
            continue
        if minute <= since_minute_prefix:
            stats["before_or_at_touch"] += 1
        else:
            stats["after_touch"] += 1

    closes = [float(c["close"]) for c in valid]
    fast = _ema_series(closes, config.EMA_FAST_PERIOD)
    slow = _ema_series(closes, config.EMA_SLOW_PERIOD)

    for i in range(1, len(closes)):
        minute = str(valid[i].get("minute") or "")
        if minute <= since_minute_prefix:
            continue
        f0, s0 = fast[i - 1], slow[i - 1]
        f1, s1 = fast[i], slow[i]
        if None in (f0, s0, f1, s1):
            continue
        prev_diff = f0 - s0
        now_diff = f1 - s1
        if prev_diff <= 0 < now_diff:
            kind = "bullish"
        elif prev_diff >= 0 > now_diff:
            kind = "bearish"
        else:
            continue
        return {
            "minute": minute,
            "timestamp": valid[i].get("timestamp"),
            "type": kind,
            "fast_ema": round(f1, 4),
            "slow_ema": round(s1, 4),
            "ema_diff": round(now_diff, 4),
            "close": closes[i],
        }, stats

    return None, stats


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class Strategy2OrderService:

    def run(
        self,
        reason: str = "manual",
        *,
        mode: str | None = None,
        catchup: bool = False,
    ) -> dict[str, Any]:
        effective_mode = str(mode or reason or "manual")
        post_market = _post_market()

        logger.info(
            "[strategy2:%s] stage_4 run (mode=%s catchup=%s post_market=%s)",
            reason, effective_mode, catchup, post_market,
        )

        if not config.STRATEGY_2_ENABLED or not config.STRATEGY_2_ORDER_ENABLED:
            return self._summary_disabled(reason, effective_mode, catchup, post_market)

        # ---- Pre-flight: classify the Stage-3 summary ---------------- #
        summary_path = _stage3_root() / "stage_3_summary.json"
        stage3_exists = summary_path.exists()
        stage3 = _read_json(summary_path) or {}
        stage3_state = _classify_stage3_summary(stage3, stage3_exists)
        isolated = stage3.get("isolated") or None
        isolated_key = (
            str(isolated.get("instrument_key") or "?") if isolated else "?"
        )

        logger.info(
            "[strategy2:%s] stage_4 stage_3 summary: state=%s exists=%s "
            "day=%s generated_at=%s isolated_key=%s",
            reason,
            stage3_state,
            stage3_exists,
            stage3.get("generated_at") or "?",
            isolated_key,
            stage3.get("day") or "?",
        )

        # ---- 1. No isolated contract (three sub-states) -------------- #
        if stage3_state == "missing":
            logger.warning(
                "[strategy2:%s] stage_4: stage_3_summary.json does NOT "
                "exist at %s. Stage 3 never ran for today, or its write "
                "failed. Check GET /admin/strategy2 and "
                "GET /admin/strategy2/tracker/status.",
                reason, summary_path,
            )
            return self._summary_no_isolated(
                reason, effective_mode, catchup, post_market,
                stage3_state=stage3_state,
            )

        if stage3_state == "no_isolated":
            logger.info(
                "[strategy2:%s] stage_4: no isolated contract from Stage 3 "
                "yet (state=%s). This is the normal 'no contract has "
                "touched its S3 level' case. Per-contract reasons are in "
                "data/strategy2/stage_3_isolation_level/"
                "stage_3_summary.json under 'contracts'.",
                reason, stage3_state,
            )
            return self._summary_no_isolated(
                reason, effective_mode, catchup, post_market,
                stage3_state=stage3_state,
            )

        if stage3_state == "no_touch_minute":
            logger.warning(
                "[strategy2:%s] stage_4: stage_3_summary.json has an "
                "isolated record but its 'touch_minute' is empty. This "
                "is a malformed record — check the file manually: %s.",
                reason, summary_path,
            )
            return self._summary_no_isolated(
                reason, effective_mode, catchup, post_market,
                stage3_state=stage3_state,
            )

        # stage3_state == "ok" — proceed to cross detection
        assert isolated is not None  # guaranteed by the classification

        instrument_key = str(isolated.get("instrument_key") or "").strip()
        touch_minute = str(isolated.get("touch_minute") or "").strip()

        # Load candles from the live EMA engine.
        from services.live_ema_engine import live_ema_engine
        payload = live_ema_engine.get_candles(instrument_key, limit=0)
        candles = payload.get("candles") or []

        cross, candle_stats = _find_first_cross_after(
            candles, since_minute_prefix=touch_minute,
        )
        if cross is None:
            # Distinguish "engine not warmed up yet" from "engine has
            # data but no cross fired". Both are still `no_cross`, but
            # the operator action differs dramatically.
            if not candle_stats.get("ema_computable"):
                logger.warning(
                    "[strategy2:%s] stage_4: no cross after %s for %s "
                    "because the EMA engine does not yet have enough "
                    "candles (usable=%d, required=%d, last=%s). "
                    "The engine is not warmed up for this contract — "
                    "check that the contract is subscribed to the feed "
                    "and that live_backfill_service ran.",
                    reason, touch_minute, instrument_key,
                    candle_stats.get("usable_candles", 0),
                    candle_stats.get("ema_required", 0),
                    candle_stats.get("last_candle_minute") or "?",
                )
            else:
                logger.info(
                    "[strategy2:%s] stage_4: no cross after %s for %s. "
                    "Candles: total=%d usable=%d before_or_at_touch=%d "
                    "after_touch=%d last=%s. The EMA engine has data "
                    "but no cross has fired yet.",
                    reason, touch_minute, instrument_key,
                    candle_stats.get("total_candles", 0),
                    candle_stats.get("usable_candles", 0),
                    candle_stats.get("before_or_at_touch", 0),
                    candle_stats.get("after_touch", 0),
                    candle_stats.get("last_candle_minute") or "?",
                )
            return self._summary_no_cross(
                reason, effective_mode, catchup, post_market,
                isolated=isolated,
                stage3_state=stage3_state,
                candle_stats=candle_stats,
            )

        # ---- 2. Cross detected — log before placing the order -------- #
        logger.info(
            "[strategy2:%s] stage_4 cross_detected: %s minute=%s "
            "type=%s close=%s fast_ema=%s slow_ema=%s ema_diff=%s "
            "(touch_minute=%s)",
            reason,
            instrument_key,
            cross.get("minute"),
            cross.get("type"),
            cross.get("close"),
            cross.get("fast_ema"),
            cross.get("slow_ema"),
            cross.get("ema_diff"),
            touch_minute,
        )

        # ---- 3. Decide order direction + instrument ----------------- #
        cross_type = str(cross.get("type") or "").lower()
        original_type = str(isolated.get("strike_type") or "").upper()
        entry_ltp = float(cross.get("close") or 0.0)
        entry_ltp_source = "cross_close"
        entry_ltp_minute = cross.get("minute")

        sibling_resolved = True  # for the summary field below

        if cross_type == "bullish":
            order_type = original_type
            order_instrument_key = instrument_key
            direction_reason = (
                f"Bullish cross on {original_type} -> BUY same type"
            )
        else:  # bearish
            order_type = _flip_type(original_type)
            order_instrument_key = self._resolve_opposite_key(isolated, order_type)
            sibling_resolved = order_instrument_key is not None

            if not sibling_resolved:
                logger.warning(
                    "[strategy2:%s] stage_4: bearish cross on %s %s %s "
                    "requires the opposite-type %s contract, but "
                    "instrument_registry.resolve(ticker=%s, strike=%s, "
                    "type=%s) returned nothing. The paper order will be "
                    "recorded with order_instrument_key=null. Fix: "
                    "check that Stage-1 resolved this strike's %s side "
                    "(data/strategy/stage_1_initial/<TICKER>/<INDEX>/"
                    "<strike>_%s/strategy_meta.json should contain a "
                    "non-empty upstox_instrument_key).",
                    reason,
                    isolated.get("ticker"),
                    isolated.get("strike_price"),
                    original_type,
                    order_type,
                    isolated.get("ticker"),
                    isolated.get("strike_price"),
                    order_type,
                    order_type,
                    order_type,
                )

            # Try to fetch the sibling's own LTP at the same minute.
            sibling_ltp = self._sibling_ltp_at(
                order_instrument_key, str(cross.get("minute") or "")
            )
            if sibling_ltp is not None:
                entry_ltp = float(sibling_ltp)
                entry_ltp_source = "sibling_ltp_at_cross_minute"
            elif sibling_resolved:
                logger.warning(
                    "[strategy2:%s] stage_4: bearish cross resolved the "
                    "opposite-type key %s, but no candle exists at "
                    "cross minute %s in its EMA buffer. Entry LTP will "
                    "fall back to the cross close of the original "
                    "contract (%s).",
                    reason,
                    order_instrument_key,
                    cross.get("minute"),
                    cross.get("close"),
                )
            direction_reason = (
                f"Bearish cross on {original_type} -> BUY opposite {order_type}"
            )

        entry_ltp_available = entry_ltp is not None and entry_ltp > 0
        if not entry_ltp_available:
            logger.warning(
                "[strategy2:%s] stage_4: entry_ltp is %r after cross "
                "detection (source=%s). The paper order will still be "
                "written but its entry price will be unusable for "
                "downstream accounting.",
                reason,
                entry_ltp,
                entry_ltp_source,
            )

        # ---- 4. Build the paper order -------------------------------- #
        order = {
            "day": isolated.get("day"),
            "strategy": "strategy_2",
            "ticker": isolated.get("ticker"),
            "index_name": isolated.get("index_name"),
            "index_key": isolated.get("index_key"),
            "isolation_level_name": isolated.get("isolation_level_name"),
            "isolation_level": isolated.get("isolation_level"),
            "s3_touch_minute": touch_minute,
            "s3_touch_ltp": isolated.get("touch_ltp"),
            "cross_minute": cross.get("minute"),
            "cross_type": cross_type,
            "original_strike_type": original_type,
            "order_strike_type": order_type,
            "order_instrument_key": order_instrument_key,
            "sibling_resolved": sibling_resolved,
            "strike_price": isolated.get("strike_price"),
            "side": "BUY",
            "quantity": config.STRATEGY_2_ORDER_QTY,
            "entry_ltp": entry_ltp,
            "entry_ltp_source": entry_ltp_source,
            "entry_ltp_minute": entry_ltp_minute,
            "entry_ltp_available": entry_ltp_available,
            "status": "paper_filled",
            "execution_mode": effective_mode,
            "catchup": bool(catchup),
            "simulation": bool(catchup),
            "post_market": post_market,
            "trigger": str(reason or mode),
            "actual_time_ist": _market_hhmmss(),
            "selected_at": _now_iso(),
            "placed_at": _now_iso(),
            "explanation": (
                f"Stage 3 isolated {isolated.get('ticker')} "
                f"{isolated.get('strike_price')} {original_type} after its "
                f"own {isolated.get('isolation_level_name')} "
                f"({isolated.get('isolation_level')}) was touched at "
                f"{touch_minute} (ltp {isolated.get('touch_ltp')}). "
                f"Stage 4 then waited for the next EMA cross on the same "
                f"contract. The first cross after the touch was "
                f"{cross_type} at {cross.get('minute')} "
                f"(close {entry_ltp}). {direction_reason}. "
                f"Entry LTP source = {entry_ltp_source}."
            ),
            "note": (
                "Paper order — real Upstox order API is not wired. "
                "Wire /v2/order/place here to go live."
            ),
        }

        # ---- 5. Persist per-contract folder + top-level summary ------ #
        base = _stage4_root()
        base.mkdir(parents=True, exist_ok=True)
        out_dir = (
            base
            / _safe(isolated.get("ticker"))
            / _safe(isolated.get("index_name"))
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        orders_path = out_dir / "orders.json"
        existing = _read_json(orders_path) or {
            "day": order["day"],
            "ticker": order["ticker"],
            "index_name": order["index_name"],
            "orders": [],
        }
        existing["orders"].append(order)
        existing["updated_at"] = _now_iso()
        _write_json(orders_path, existing)

        meta_path = out_dir / "order_meta_test.json"
        _write_json(meta_path, {
            "strategy": "strategy_2",
            "stage": "stage_4",
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "day": order["day"],
            "order": order,
            "audit": {
                "isolation_level": isolated.get("isolation_level_name"),
                "s3_touch_minute": touch_minute,
                "cross_minute": cross.get("minute"),
                "cross_type": cross_type,
                "direction_rule": direction_reason,
                "candle_stats": candle_stats,
            },
        })

        summary = {
            "status": "ok",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason,
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "generated_at": _now_iso(),
            "day": order["day"],
            "isolated": isolated,
            "cross": cross,
            "placed": 1,
            "order": order,
            # ---- new diagnostic fields ---------------------------------- #
            "stage_3_summary_state": stage3_state,
            "sibling_resolved": sibling_resolved,
            "entry_ltp_available": entry_ltp_available,
            "candle_stats": candle_stats,
        }
        _write_json(base / "stage_4_summary.json", summary)

        logger.info(
            "[strategy2:%s] stage_4 COMPLETE — placed=1 ticker=%s "
            "strike=%s type=%s cross=%s @ %s entry_ltp=%s source=%s "
            "sibling_resolved=%s",
            reason,
            order["ticker"],
            order["strike_price"],
            order["order_strike_type"],
            cross_type,
            cross.get("minute"),
            order.get("entry_ltp"),
            order.get("entry_ltp_source"),
            sibling_resolved,
        )
        return summary

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _resolve_opposite_key(
        self, isolated: dict[str, Any], opposite_type: str
    ) -> str | None:
        """Lookup the sibling (opposite-type) contract from the registry."""
        ticker = isolated.get("ticker")
        strike = isolated.get("strike_price")
        if not ticker or strike is None:
            return None
        try:
            entry = instrument_registry.resolve(
                str(ticker), strike, opposite_type
            )
            if entry:
                return entry["instrument_key"]
        except Exception:
            logger.exception(
                "[strategy2] opposite-key resolve failed for %s %s %s",
                ticker, strike, opposite_type,
            )
        return None

    def _sibling_ltp_at(
        self, instrument_key: str | None, minute: str
    ) -> float | None:
        """Return the sibling contract's close at the given minute (best effort)."""
        if not instrument_key or not minute:
            return None
        try:
            from services.live_ema_engine import live_ema_engine
            payload = live_ema_engine.get_candles(instrument_key, limit=0)
            for c in payload.get("candles") or []:
                if str(c.get("minute")) == minute:
                    try:
                        return float(c.get("close"))
                    except (TypeError, ValueError):
                        return None
        except Exception:
            logger.exception(
                "[strategy2] sibling LTP fetch failed for %s",
                instrument_key,
            )
        return None

    # ------------------------------------------------------------------ #
    # Per-status summaries                                               #
    # ------------------------------------------------------------------ #
    def _summary_disabled(self, reason, mode, catchup, post_market) -> dict[str, Any]:
        return {
            "status": "disabled",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason, "mode": mode,
            "catchup": bool(catchup), "post_market": post_market,
            "generated_at": _now_iso(),
            "placed": 0, "order": None,
            # ---- new diagnostic fields ---------------------------------- #
            "stage_3_summary_state": None,
            "sibling_resolved": None,
            "entry_ltp_available": None,
            "candle_stats": None,
        }

    def _summary_no_isolated(
        self, reason, mode, catchup, post_market,
        *, stage3_state: str = "no_isolated",
    ) -> dict[str, Any]:
        return {
            "status": "no_isolated",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason, "mode": mode,
            "catchup": bool(catchup), "post_market": post_market,
            "generated_at": _now_iso(),
            "placed": 0, "order": None,
            # ---- new diagnostic fields ---------------------------------- #
            "stage_3_summary_state": stage3_state,
            "sibling_resolved": None,
            "entry_ltp_available": None,
            "candle_stats": None,
        }

    def _summary_no_cross(
        self, reason, mode, catchup, post_market,
        *, isolated, stage3_state: str = "ok",
        candle_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "no_cross_after_touch",
            "stage": "stage_4",
            "strategy": "strategy_2",
            "reason": reason, "mode": mode,
            "catchup": bool(catchup), "post_market": post_market,
            "generated_at": _now_iso(),
            "isolated": isolated,
            "placed": 0, "order": None,
            # ---- new diagnostic fields ---------------------------------- #
            "stage_3_summary_state": stage3_state,
            "sibling_resolved": None,
            "entry_ltp_available": None,
            "candle_stats": candle_stats or {},
        }


strategy_2_order_service = Strategy2OrderService()