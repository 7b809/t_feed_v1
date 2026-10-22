# File: services/strategy_isolation_service.py
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

Diagnostics (added for the 2026-10-22 ``no_activations`` bug)
-------------------------------------------------------------
The module used to write nothing to ``logs/strategy_isolation_service.log``
— its logger was never attached to the file handlers. On 2026-10-22
Stage 2 succeeded (Telegram showed ``isolated=68 pairs=34``) but the
log stream contained no line saying so, and no line warning that the
68 isolated strike folders were missing their own ``opening_range.json``
(the file the S1/S2 trackers read to learn each contract's R2/S3
level). Stage 4 then reported ``no_activations`` with no explanation.

This module now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers and can be grepped afterwards;
    * counts ``contracts_missing_or`` per index — the number of
      isolated contracts whose source strike folder has no
      ``opening_range.json`` with a real ``levels.R2``. These are
      exactly the contracts the S1/S2 trackers will skip
      (``isolation_level = None``), and Stage 4 will report as
      ``no_activations`` for them;
    * counts ``contracts_missing_key`` per index — isolated contracts
      with no ``upstox_instrument_key`` (unresolved, cannot be
      subscribed or traded);
    * emits one WARNING per index when either counter is non-zero,
      naming the count, up to five example folder names, and pointing
      at ``strategy_selection_service._enrich_with_upstox`` and
      ``opening_range_service.calculate_one(target_folder=...)``;
    * emits one WARNING per index when ``isolated == 0`` (Stage 2
      produced nothing → Stage 4 will be empty);
    * emits one INFO per index otherwise (the normal case);
    * carries the time remaining until Stage 4 (``stage_4 in Ns``) on
      the top-level COMPLETE line, so the timing budget between the
      09:17:00 Stage-2 cron and the 09:18:00 Stage-4 cron is visible.

Catch-up semantics
------------------
The unified refresh calls this with ``mode=reason`` and
``catchup=True`` when the project starts (or daily / hard refresh
fires) after the scheduled 09:17:00 IST time and still inside the
catch-up window (09:15 – 23:59 IST). The summary records:

    mode              : startup | daily | manual | scheduled
    catchup           : bool
    post_market       : bool (true when the run happens after 15:30 IST)
    scheduled_time_ist: "09:17:00"
    actual_time_ist   : "HH:MM:SS"
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core import config
from core.logger import get_logger
from services.token_service import token_service

logger = get_logger(__file__)

_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 20

# Cash-market close in the market timezone. Anything after this is
# recorded as post-market in the summary.
_MARKET_CLOSE_TIME = dtime(15, 30)


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
    """True after the Indian cash-market close (15:30 IST)."""
    return _market_now().time() > _MARKET_CLOSE_TIME


def _seconds_to_stage_4() -> int | None:
    """Return whole seconds remaining until the scheduled Stage-4 time.

    ``None`` on any calculation failure. Used purely for diagnostic
    log lines — Stage-4 fires independently of this value.
    """
    try:
        now = _market_now()
        target = now.replace(
            hour=config.STRATEGY_ORDER_HOUR,
            minute=config.STRATEGY_ORDER_MINUTE,
            second=0,
            microsecond=0,
        )
        delta = (target - now).total_seconds()
        return int(delta)
    except Exception:
        return None


def _scheduled_stage_2_hhmm() -> str:
    """09:17:00 IST by default (OR + 50 s)."""
    base = (
        config.OPENING_RANGE_HOUR * 3600
        + config.OPENING_RANGE_MINUTE * 60
        + config.OPENING_RANGE_SECOND
    )
    total = (base + 50) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _strike_folder_has_or_levels(
    strike_dir: Path | None,
    level_key: str = "R2",
) -> bool:
    """Return True when ``strike_dir/opening_range.json`` carries a real level.

    The S1/S2 trackers read this file to learn each contract's own
    R2 / S3 level. A folder without it produces ``isolation_level =
    None`` downstream, and the contract is silently skipped by both
    trackers. This helper lets Stage 2 warn about that state *before*
    Stage 4 discovers it via ``no_activations``.
    """
    if strike_dir is None:
        return False
    try:
        or_path = Path(strike_dir) / "opening_range.json"
        if not or_path.exists():
            return False
        payload = _read_json(or_path) or {}
        levels = payload.get("levels") or {}
        return levels.get(level_key) is not None
    except Exception:
        return False


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
    def run(
        self,
        reason: str = "manual",
        *,
        mode: str | None = None,
        catchup: bool = False,
    ) -> dict[str, Any]:
        """Stage 2 entry point.

        Parameters
        ----------
        reason  : trigger label ("startup" / "daily" / "manual" / "scheduled")
        mode    : alias for reason; separate so a caller can pass a distinct
                  scheduled mode.
        catchup : True when the run is a late catch-up.
        """
        effective_mode = str(mode or reason or "manual")
        post_market = _post_market()
        scheduled_hhmm = _scheduled_stage_2_hhmm()
        actual_hhmmss = _market_hhmmss()

        logger.info(
            "[strategy:%s] stage_2 run (mode=%s catchup=%s post_market=%s "
            "scheduled=%s actual=%s)",
            reason,
            effective_mode,
            catchup,
            post_market,
            scheduled_hhmm,
            actual_hhmmss,
        )

        if not config.STRATEGY_ENABLED or not config.STRATEGY_ISOLATION_ENABLED:
            return {
                "status": "disabled",
                "stage": "stage_2",
                "reason": reason,
                "mode": effective_mode,
                "catchup": bool(catchup),
                "post_market": post_market,
                "scheduled_time_ist": scheduled_hhmm,
                "actual_time_ist": actual_hhmmss,
                "generated_at": _now_iso(),
                "isolated": 0,
                "pairs_isolated": 0,
                "evaluated": 0,
                "skipped": 0,
                "errors": 0,
                "contracts_missing_or": 0,
                "contracts_missing_key": 0,
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
                "mode": effective_mode,
                "catchup": bool(catchup),
                "post_market": post_market,
                "scheduled_time_ist": scheduled_hhmm,
                "actual_time_ist": actual_hhmmss,
                "generated_at": _now_iso(),
                "isolated": 0,
                "pairs_isolated": 0,
                "evaluated": 0,
                "skipped": 0,
                "errors": 0,
                "contracts_missing_or": 0,
                "contracts_missing_key": 0,
                "indexes": [],
            }

        today_iso = date.today().isoformat()

        logger.info(
            "[strategy:%s] stage_2 START — today=%s lookback=%dm pairing=on "
            "(mode=%s catchup=%s post_market=%s)",
            reason,
            today_iso,
            config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
            effective_mode,
            catchup,
            post_market,
        )

        per_index: list[dict[str, Any]] = []
        total_evaluated = 0
        total_isolated = 0
        total_pairs = 0
        total_skipped = 0
        total_errors = 0
        total_missing_or = 0
        total_missing_key = 0

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
                        reason=reason,
                    )
                except Exception as exc:
                    logger.exception(
                        "[strategy:%s] stage_2 %s/%s failed",
                        reason,
                        ticker_dir.name,
                        index_dir.name,
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
                        "contracts_missing_or": 0,
                        "contracts_missing_key": 0,
                        "contracts": [],
                    }
                per_index.append(idx_result)
                total_evaluated += idx_result.get("evaluated", 0)
                total_isolated += idx_result.get("isolated", 0)
                total_pairs += idx_result.get("pairs_isolated", 0)
                total_skipped += idx_result.get("skipped", 0)
                total_errors += idx_result.get("errors", 0)
                total_missing_or += idx_result.get("contracts_missing_or", 0)
                total_missing_key += idx_result.get("contracts_missing_key", 0)

        summary = {
            "status": "ok",
            "stage": "stage_2",
            "reason": reason,
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "scheduled_time_ist": scheduled_hhmm,
            "actual_time_ist": _market_hhmmss(),
            "generated_at": _now_iso(),
            "today": today_iso,
            "lookback_minutes": config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
            "pairing": "strike_ce_pe",
            "evaluated": total_evaluated,
            "isolated": total_isolated,
            "pairs_isolated": total_pairs,
            "skipped": total_skipped,
            "errors": total_errors,
            # ---- new diagnostic counters ------------------------------ #
            "contracts_missing_or": total_missing_or,
            "contracts_missing_key": total_missing_key,
            "indexes": per_index,
        }

        try:
            stage2_base.mkdir(parents=True, exist_ok=True)
            _write_json(stage2_base / "strategy_isolation.json", summary)
        except Exception:
            logger.exception("[strategy:%s] stage_2 summary write failed", reason)

        seconds_to_stage4 = _seconds_to_stage_4()
        logger.info(
            "[strategy:%s] stage_2 COMPLETE — evaluated=%d isolated=%d "
            "pairs=%d skipped=%d errors=%d missing_or=%d missing_key=%d "
            "(mode=%s catchup=%s post_market=%s, stage_4 in %ss)",
            reason,
            total_evaluated,
            total_isolated,
            total_pairs,
            total_skipped,
            total_errors,
            total_missing_or,
            total_missing_key,
            effective_mode,
            catchup,
            post_market,
            str(seconds_to_stage4) if seconds_to_stage4 is not None else "?",
        )

        # ---- Aggregate WARNING when the outcome is the Oct 22 failure # #
        # The per-index log lines already flag this, but a single
        # aggregate line makes the state impossible to miss in a grep.
        if total_missing_or > 0:
            logger.warning(
                "[strategy:%s] stage_2 isolated %d contracts but %d of "
                "them have NO opening_range.json with levels.R2 in their "
                "strike folder. The S1/S2 trackers will report "
                "isolation_level=None for these contracts and Stage 4 "
                "will report no_activations for them. Fix: verify "
                "services/strategy_selection_service.py's "
                "_enrich_with_upstox writes the strike OR via "
                "opening_range_service.calculate_one(key, "
                "target_folder=<strike folder>).",
                reason,
                total_isolated,
                total_missing_or,
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
        reason: str = "manual",
    ) -> dict[str, Any]:
        ticker = ticker_dir.name
        index_name = index_dir.name

        strike_dirs = [p for p in sorted(index_dir.iterdir()) if p.is_dir()]
        token = token_service.get_access_token()

        # ---- 1. Evaluate each contract independently -------------------- #
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
                        "[strategy] stage_2 %s failed",
                        strike_dir.name,
                    )
                    evaluations.append(
                        {
                            "status": "error",
                            "stage_1_folder": str(strike_dir),
                            "error": f"{type(exc).__name__}: {exc}",
                            "strike_price": None,
                            "strike_type": None,
                        }
                    )

        # ---- 2. Group by strike price to apply pairing ------------------ #
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

        isolated_contracts: list[dict[str, Any]] = []  # to copy
        skipped_contracts: list[dict[str, Any]] = []  # to report
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
                    paired["pair_reason"] = f"sibling_{trigger_side.lower()}_triggered"
                    isolated_contracts.append(paired)

        # ---- 3. Copy isolated contracts into stage_2 -------------------- #
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
                    "[strategy] stage_2 copy failed for %s",
                    strike_dir,
                )
                errors += 1

        # ---- 4. Diagnostics: strike folder completeness ---------------- #
        # This is where Stage 2 warns about the Oct 22 state: isolated
        # contracts whose strike folder carries no opening_range.json
        # with levels.R2. The S1/S2 trackers read that file to learn
        # each contract's own R2/S3 level; without it, every contract
        # is silently skipped and Stage 4 reports no_activations.
        missing_or_contracts: list[str] = []
        missing_key_contracts: list[str] = []
        for ev in isolated_contracts:
            src = ev.get("stage_1_folder")
            src_name = Path(src).name if src else "?"
            if not _strike_folder_has_or_levels(
                Path(src) if src else None, level_key="R2"
            ):
                missing_or_contracts.append(src_name)
            if not ev.get("upstox_instrument_key"):
                missing_key_contracts.append(src_name)

        # ---- 5. Per-index summary --------------------------------------- #
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
            # ---- new diagnostic counters ------------------------------- #
            "contracts_missing_or": len(missing_or_contracts),
            "contracts_missing_key": len(missing_key_contracts),
            "missing_or_examples": missing_or_contracts[:5],
            "missing_key_examples": missing_key_contracts[:5],
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
                ticker,
                index_name,
            )

        # ---- 6. Per-index log line -------------------------------------- #
        # Level choice:
        #   * WARNING when missing_or > 0   (exact Oct 22 signature)
        #   * WARNING when isolated == 0    (Stage 4 will be empty)
        #   * INFO    otherwise
        log_level = 20  # logging.INFO, spelled numerically to avoid
                        # a top-level `import logging` just for this
        if missing_or_contracts:
            log_level = 30  # logging.WARNING
        elif isolated == 0:
            log_level = 30  # logging.WARNING

        logger.log(
            log_level,
            "[strategy:%s] stage_2 %s/%s: evaluated=%d isolated=%d "
            "pairs=%d skipped=%d errors=%d missing_or=%d missing_key=%d",
            reason,
            ticker,
            index_name,
            evaluated,
            isolated,
            pairs_isolated,
            skipped,
            errors,
            len(missing_or_contracts),
            len(missing_key_contracts),
        )

        # ---- 7. Detailed WARNING for missing OR ------------------------ #
        if missing_or_contracts:
            logger.warning(
                "[strategy:%s] stage_2 %s/%s: %d/%d isolated strike "
                "folder(s) have NO opening_range.json with levels.R2 — "
                "the S1/S2 trackers will skip these contracts and "
                "Stage 4 will report no_activations for them. "
                "Examples: %s. "
                "Fix: check services/strategy_selection_service.py's "
                "_enrich_with_upstox wrote each strike's OR via "
                "opening_range_service.calculate_one(key, "
                "target_folder=<strike folder>).",
                reason,
                ticker,
                index_name,
                len(missing_or_contracts),
                isolated,
                ", ".join(missing_or_contracts[:5]),
            )

        # ---- 8. Detailed WARNING for missing key ---------------------- #
        if missing_key_contracts:
            logger.warning(
                "[strategy:%s] stage_2 %s/%s: %d/%d isolated contract(s) "
                "have NO upstox_instrument_key — they cannot be "
                "subscribed to the live feed or traded. Examples: %s. "
                "Fix: check that Stage-1 resolved Upstox keys for these "
                "strikes (Upstox search returned no match).",
                reason,
                ticker,
                index_name,
                len(missing_key_contracts),
                isolated,
                ", ".join(missing_key_contracts[:5]),
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

        # ---- 0. Need a real Upstox key to fetch today's intraday ------- #
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

        # ---- 1. Compute today's first-candle cross --------------------- #
        today_cross = self._compute_first_candle_cross(
            strike_dir=strike_dir,
            upstox_key=upstox_key,
            today_iso=today_iso,
            token=token,
        )
        today_type = today_cross.get("type") if today_cross else None

        # ---- 2. Apply rules -------------------------------------------- #
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
        history = [c for c in history if str(c.get("timestamp", ""))[:10] < today_iso]

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
                c
                for c in today_candles
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