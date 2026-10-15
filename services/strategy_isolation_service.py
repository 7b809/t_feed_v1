"""Stage 2 — EMA-cross isolation with strike-level pairing.

Reads the stage-1 tree at ``data/strategy/stage_1_initial`` and, for
each strike folder that was resolved to a real Upstox key, checks:

    1. ``last_day_cross.json`` — the last cross inside the tail window
       of the last trading day (computed by stage 1).
    2. Today's first 1-minute candle (09:15 – 09:16 IST) — was there a
       fast/slow EMA cross on it?

It then applies the bullish-priority selection rules:

    Case 1  last day bearish  + today bullish   -> isolate
    Case 2  last day bullish  + today none      -> isolate
    Case 3  last day none     + today any       -> isolate
    Case 4  last day none     + today none      -> NOT isolated

Additional combinations handled by the same priority rule:

    last day bullish + today bullish -> isolate
    last day bullish + today bearish -> isolate (bullish priority)
    last day bearish + today none    -> NOT isolated (bearish alone)
    last day bearish + today bearish -> NOT isolated

Strike-level pairing
--------------------

The rule is decided per contract (CE / PE), but the OUTPUT is paired:
if either the CE or the PE folder of a given strike isolates, **both
sides** are copied to ``stage_2_isolation``. This keeps a selected
strike group (call + put) together so downstream consumers always see
a complete strike.

The side that triggered the isolation keeps its normal
``isolation_meta.json`` (``decision: "isolate"``). The paired sibling
is copied with ``decision: "isolate_paired"`` and
``paired_trigger: "CE"`` (or ``"PE"``) so you can tell from disk which
side decided.

Output layout::

    data/strategy/stage_2_isolation/<TICKER>/<INDEX_NAME>/<strike>_<TYPE>/

Each isolated folder contains every stage-1 file plus an
``isolation_meta.json`` describing the decision.
"""

from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from core import config
from services.token_service import token_service

logger = logging.getLogger(__name__)

_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ema(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------
def _should_isolate(
    last_day_type: str | None,
    today_type: str | None,
) -> tuple[bool, str]:
    """Return (isolate?, reason_code).

    The rules implement the bullish-priority logic spelled out in the
    spec, plus the natural extensions of that logic.
    """
    if last_day_type is None and today_type is None:
        return False, "case_4_both_none"

    if last_day_type == "bearish" and today_type == "bullish":
        return True, "case_1_bullish_after_bearish"

    if last_day_type == "bullish" and today_type is None:
        return True, "case_2_bullish_carries"

    if last_day_type is None and today_type is not None:
        return True, "case_3_fresh_cross_today"

    if last_day_type == "bullish" and today_type == "bullish":
        return True, "both_bullish"

    if last_day_type == "bullish" and today_type == "bearish":
        return True, "bullish_priority_over_today_bearish"

    if last_day_type == "bearish" and today_type is None:
        return False, "bearish_alone"

    if last_day_type == "bearish" and today_type == "bearish":
        return False, "bearish_both"

    return False, "unhandled"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class StrategyIsolationService:
    """Stage 2 — EMA-cross isolation onto stage_2_isolation."""

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def run(self, reason: str = "manual") -> dict[str, Any]:
        if not config.STRATEGY_ENABLED or not config.STRATEGY_ISOLATION_ENABLED:
            return {
                "status": "disabled",
                "stage": "stage_2",
                "reason": reason,
                "generated_at": _now_iso(),
                "isolated": 0,
                "pairs_isolated": 0,
                "evaluated": 0,
                "skipped": 0,
                "errors": 0,
                "indexes": [],
            }

        stage1_base = Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_1_NAME
        stage2_base = Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_2_NAME

        if not stage1_base.exists():
            logger.info("[strategy:%s] stage_2: no stage_1 output yet", reason)
            return {
                "status": "no_stage_1",
                "stage": "stage_2",
                "reason": reason,
                "generated_at": _now_iso(),
                "isolated": 0,
                "pairs_isolated": 0,
                "evaluated": 0,
                "skipped": 0,
                "errors": 0,
                "indexes": [],
            }

        today_iso = date.today().isoformat()

        logger.info(
            "[strategy:%s] stage_2 START — today=%s lookback=%dm pairing=on",
            reason, today_iso, config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
        )

        per_index: list[dict[str, Any]] = []
        total_evaluated = 0
        total_isolated = 0
        total_pairs = 0
        total_skipped = 0
        total_errors = 0

        for ticker_dir in sorted(stage1_base.iterdir()):
            if not ticker_dir.is_dir():
                continue
            for index_dir in sorted(ticker_dir.iterdir()):
                if not index_dir.is_dir():
                    continue
                try:
                    idx_result = self._process_index(
                        ticker_dir=ticker_dir,
                        index_dir=index_dir,
                        stage2_base=stage2_base,
                        today_iso=today_iso,
                    )
                except Exception as exc:
                    logger.exception(
                        "[strategy:%s] stage_2 %s/%s failed",
                        reason, ticker_dir.name, index_dir.name,
                    )
                    idx_result = {
                        "status": "error",
                        "ticker": ticker_dir.name,
                        "index_name": index_dir.name,
                        "error": f"{type(exc).__name__}: {exc}",
                        "evaluated": 0,
                        "isolated": 0,
                        "pairs_isolated": 0,
                        "skipped": 0,
                        "errors": 1,
                        "contracts": [],
                    }
                per_index.append(idx_result)
                total_evaluated += idx_result.get("evaluated", 0)
                total_isolated += idx_result.get("isolated", 0)
                total_pairs += idx_result.get("pairs_isolated", 0)
                total_skipped += idx_result.get("skipped", 0)
                total_errors += idx_result.get("errors", 0)

        summary = {
            "status": "ok",
            "stage": "stage_2",
            "reason": reason,
            "generated_at": _now_iso(),
            "today": today_iso,
            "lookback_minutes": config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
            "pairing": "strike_ce_pe",
            "evaluated": total_evaluated,
            "isolated": total_isolated,
            "pairs_isolated": total_pairs,
            "skipped": total_skipped,
            "errors": total_errors,
            "indexes": per_index,
        }

        try:
            stage2_base.mkdir(parents=True, exist_ok=True)
            _write_json(stage2_base / "strategy_isolation.json", summary)
        except Exception:
            logger.exception("[strategy:%s] stage_2 summary write failed", reason)

        logger.info(
            "[strategy:%s] stage_2 COMPLETE — evaluated=%d isolated=%d "
            "pairs=%d skipped=%d errors=%d",
            reason, total_evaluated, total_isolated,
            total_pairs, total_skipped, total_errors,
        )
        return summary

    # ------------------------------------------------------------------ #
    # Per-index processing                                               #
    # ------------------------------------------------------------------ #
    def _process_index(
        self,
        *,
        ticker_dir: Path,
        index_dir: Path,
        stage2_base: Path,
        today_iso: str,
    ) -> dict[str, Any]:
        ticker = ticker_dir.name
        index_name = index_dir.name

        strike_dirs = [p for p in sorted(index_dir.iterdir()) if p.is_dir()]
        token = token_service.get_access_token()

        # ---- 1. Evaluate each contract independently --------------------
        evaluations: list[dict[str, Any]] = []
        workers = min(config.STRATEGY_MAX_WORKERS, max(1, len(strike_dirs)))

        def _task(strike_dir: Path) -> dict[str, Any]:
            return self._evaluate_strike(
                strike_dir=strike_dir,
                ticker=ticker,
                index_name=index_name,
                today_iso=today_iso,
                token=token,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, sd): sd for sd in strike_dirs}
            for fut in as_completed(futures):
                strike_dir = futures[fut]
                try:
                    evaluations.append(fut.result())
                except Exception as exc:
                    logger.exception(
                        "[strategy] stage_2 %s failed", strike_dir.name,
                    )
                    evaluations.append({
                        "status": "error",
                        "stage_1_folder": str(strike_dir),
                        "error": f"{type(exc).__name__}: {exc}",
                        "strike_price": None,
                        "strike_type": None,
                    })

        # ---- 2. Group by strike price to apply pairing ------------------
        # A strike is "selected" if at least one of its two contracts
        # (CE, PE) has isolated == True. Both sides of a selected strike
        # are then copied to stage_2_isolation.
        by_strike: dict[Any, dict[str, dict[str, Any]]] = {}
        for ev in evaluations:
            sp = ev.get("strike_price")
            st = str(ev.get("strike_type") or "").upper()
            if sp is None or not st:
                continue
            by_strike.setdefault(sp, {})[st] = ev

        isolated_contracts: list[dict[str, Any]] = []   # to copy
        skipped_contracts: list[dict[str, Any]] = []    # to report
        errors = 0
        pairs_isolated = 0

        for sp, sides in by_strike.items():
            trigger_side = None
            for side in ("CE", "PE"):
                ev = sides.get(side)
                if ev and ev.get("isolated"):
                    trigger_side = side
                    break

            if trigger_side is None:
                # No side triggered — the whole strike stays in stage 1.
                for side, ev in sides.items():
                    if ev.get("status") == "error":
                        errors += 1
                    skipped_contracts.append(ev)
                continue

            # At least one side triggered: BOTH go to stage 2.
            pairs_isolated += 1
            for side, ev in sides.items():
                if ev.get("status") == "error":
                    errors += 1
                    skipped_contracts.append(ev)
                    continue
                if side == trigger_side:
                    # Keep as-is; decision already "isolate".
                    isolated_contracts.append(ev)
                else:
                    # Pair the sibling in.
                    paired = dict(ev)
                    paired["status"] = "isolated"
                    paired["isolated"] = True
                    paired["decision"] = "isolate_paired"
                    paired["paired_trigger"] = trigger_side
                    paired["pair_reason"] = (
                        f"sibling_{trigger_side.lower()}_triggered"
                    )
                    isolated_contracts.append(paired)

        # ---- 3. Copy isolated contracts into stage_2 --------------------
        isolated = 0
        for ev in isolated_contracts:
            src_folder = ev.get("stage_1_folder")
            if not src_folder:
                errors += 1
                continue
            strike_dir = Path(src_folder)
            out_dir = stage2_base / ticker / index_name / strike_dir.name
            try:
                if out_dir.exists():
                    shutil.rmtree(out_dir)
                shutil.copytree(strike_dir, out_dir)
                _write_json(out_dir / "isolation_meta.json", ev)
                ev["stage_2_folder"] = str(out_dir)
                isolated += 1
            except Exception:
                logger.exception(
                    "[strategy] stage_2 copy failed for %s", strike_dir,
                )
                errors += 1

        # ---- 4. Per-index summary ---------------------------------------
        all_contracts = isolated_contracts + skipped_contracts
        evaluated = len(all_contracts)
        skipped = len(skipped_contracts)

        idx_summary = {
            "status": "ok",
            "stage": "stage_2",
            "ticker": ticker,
            "index_name": index_name,
            "today": today_iso,
            "pairing": "strike_ce_pe",
            "evaluated": evaluated,
            "isolated": isolated,
            "pairs_isolated": pairs_isolated,
            "skipped": skipped,
            "errors": errors,
            "contracts": all_contracts,
            "generated_at": _now_iso(),
        }

        try:
            idx_out = stage2_base / ticker / index_name
            idx_out.mkdir(parents=True, exist_ok=True)
            _write_json(idx_out / "stage_2_summary.json", idx_summary)
        except Exception:
            logger.exception(
                "[strategy] stage_2 per-index summary write failed for %s/%s",
                ticker, index_name,
            )

        return idx_summary

    # ------------------------------------------------------------------ #
    # Per-strike evaluation (unchanged logic, per-contract verdict)      #
    # ------------------------------------------------------------------ #
    def _evaluate_strike(
        self,
        *,
        strike_dir: Path,
        ticker: str,
        index_name: str,
        today_iso: str,
        token: str | None,
    ) -> dict[str, Any]:
        meta = _read_json(strike_dir / "strategy_meta.json") or {}
        last_day = _read_json(strike_dir / "last_day_cross.json") or {}

        last_day_type = last_day.get("type")
        upstox_key = meta.get("upstox_instrument_key")

        base = {
            "stage": "stage_2",
            "ticker": ticker,
            "index_name": index_name,
            "strike_price": meta.get("strike_price"),
            "strike_type": meta.get("strike_type"),
            "upstox_instrument_key": upstox_key,
            "stage_1_folder": str(strike_dir),
            "today": today_iso,
            "last_day_cross": last_day,
            "evaluated_at": _now_iso(),
        }

        # ---- 0. Need a real Upstox key to fetch today's intraday -------
        if not upstox_key:
            if config.STRATEGY_ISOLATION_SKIP_UNRESOLVED:
                return {
                    **base,
                    "status": "skipped",
                    "reason": "no_upstox_key",
                    "today_first_cross": None,
                    "isolated": False,
                    "decision": "skipped_unresolved",
                }
            return {
                **base,
                "status": "error",
                "reason": "no_upstox_key",
                "decision": "error",
            }

        if not token:
            return {
                **base,
                "status": "skipped",
                "reason": "no_token",
                "today_first_cross": None,
                "isolated": False,
                "decision": "skipped_no_token",
            }

        # ---- 1. Compute today's first-candle cross ---------------------
        today_cross = self._compute_first_candle_cross(
            strike_dir=strike_dir,
            upstox_key=upstox_key,
            today_iso=today_iso,
            token=token,
        )
        today_type = today_cross.get("type") if today_cross else None

        # ---- 2. Apply rules --------------------------------------------
        should, reason_code = _should_isolate(last_day_type, today_type)

        return {
            **base,
            "status": "isolated" if should else "skipped",
            "reason": reason_code,
            "last_day_type": last_day_type,
            "today_type": today_type,
            "today_first_cross": today_cross,
            "isolated": should,
            "decision": ("isolate" if should else "not_isolated"),
        }

    # ------------------------------------------------------------------ #
    # Today's first-candle cross detection                               #
    # ------------------------------------------------------------------ #
    def _compute_first_candle_cross(
        self,
        *,
        strike_dir: Path,
        upstox_key: str,
        today_iso: str,
        token: str,
    ) -> dict[str, Any] | None:
        """Return the cross record if the 09:15 candle produced one."""
        hist_payload = _read_json(strike_dir / "candles.json") or {}
        history = hist_payload.get("candles") or []
        history = [
            c for c in history
            if str(c.get("timestamp", ""))[:10] < today_iso
        ]

        try:
            resp = requests.get(
                f"{_UPSTOX_BASE}/v3/historical-candle/intraday/"
                f"{upstox_key}/minutes/1",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return None
        if resp.status_code >= 400:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None

        raw = (
            (body.get("data") or {}).get("candles")
            if isinstance(body.get("data"), dict)
            else body.get("candles")
        ) or []
        today_candles: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                today_candles.append(
                    {
                        "timestamp": str(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]) if len(row) >= 6 else 0.0,
                    }
                )
            except (TypeError, ValueError):
                continue

        today_candles.sort(key=lambda c: c["timestamp"])
        first_today = next(
            (
                c for c in today_candles
                if str(c["timestamp"]).startswith(f"{today_iso}T09:15")
            ),
            None,
        )
        if first_today is None:
            return None

        merged = list(history) + [first_today]
        if len(merged) < max(config.EMA_SLOW_PERIOD + 2, 3):
            return None

        closes = [float(c["close"]) for c in merged]
        fast = _ema(closes, config.EMA_FAST_PERIOD)
        slow = _ema(closes, config.EMA_SLOW_PERIOD)

        i = len(closes) - 1
        if i < 1:
            return None
        f0, s0 = fast[i - 1], slow[i - 1]
        f1, s1 = fast[i], slow[i]
        if None in (f0, s0, f1, s1):
            return None

        prev_diff = f0 - s0
        now_diff = f1 - s1
        if prev_diff <= 0 < now_diff:
            kind = "bullish"
        elif prev_diff >= 0 > now_diff:
            kind = "bearish"
        else:
            return None

        return {
            "timestamp": first_today["timestamp"],
            "type": kind,
            "fast_ema": round(f1, 4),
            "slow_ema": round(s1, 4),
            "ema_diff": round(now_diff, 4),
            "close": closes[i],
            "volume": first_today.get("volume", 0.0),
        }


strategy_isolation_service = StrategyIsolationService()