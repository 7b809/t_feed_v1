"""STRATEGY 2 — Stage 3: isolation-level touch + single-instrument filter.

Reads Stage 2 output (shared with Strategy 1) and, for each isolated
contract, computes that contract's OWN isolation level from its OWN
opening_range.json (S3 by default, configurable to S1/S2/S3/R1/R2/R3).

A "touch" for a contract is recorded when its LTP reaches or crosses its
own isolation level during the day (default window 09:15–15:30 IST).
Only the FIRST touch per contract is kept.

After every new touch, the service re-runs the filter and writes:

    data/strategy2/stage_3_isolation_level/
        stage_3_summary.json
        <TICKER>/<INDEX>/<strike>_<TYPE>/
            s3_touch.json          <- per-contract touch record

The filter reduces all touched contracts to ONE final isolated contract:

    1) if exactly one touched -> that one
    2) else filter by STRATEGY_2_TICKER_PRIORITY (default NIFTY > SENSEX)
    3) within the winning ticker, keep the strike nearest to that
       index's ATM (|strike - index_ltp| minimum)

The chosen contract is written into stage_3_summary.json under
"isolated" so Stage 4 can pick it up.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config

logger = logging.getLogger(__name__)

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

def _isolation_level_key() -> str:
    """Return the configured isolation level (S3 by default)."""
    return (config.STRATEGY_2_ISOLATION_LEVEL or "S3").upper()

def _stage2_root() -> Path:
    return Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_2_NAME

def _stage3_root() -> Path:
    return Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_3_NAME

def _stage1_root() -> Path:
    return Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_1_NAME

# ---------------------------------------------------------------------------
# Isolation-level lookup
# ---------------------------------------------------------------------------
def _read_contract_isolation_level(
    *, ticker: str, index_name: str, strike_dir_name: str
) -> tuple[float | None, dict[str, Any]]:
    """Return (level_value, levels_dict) for the contract's own OR file."""
    or_path = (
        _stage1_root()
        / _safe(ticker)
        / _safe(index_name)
        / strike_dir_name
        / "opening_range.json"
    )
    payload = _read_json(or_path) or {}
    levels = payload.get("levels") or {}
    key = _isolation_level_key()
    try:
        value = levels.get(key)
        if value is None:
            return None, levels
        return float(value), levels
    except (TypeError, ValueError):
        return None, levels

# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------
def _ticker_priority(ticker: str) -> int:
    """Lower number = higher priority. Unknown tickers last."""
    order = [str(t).upper() for t in (config.STRATEGY_2_TICKER_PRIORITY or [])]
    t = str(ticker or "").upper()
    try:
        return order.index(t)
    except ValueError:
        return len(order) + 1

def _strike_distance_to_index_ltp(
    contract: dict[str, Any], index_ltp: float | None
) -> float:
    """Return |strike - index_ltp|. Falls back to |strike| when LTP missing."""
    try:
        strike = float(contract.get("strike_price") or 0)
    except (TypeError, ValueError):
        return float("inf")
    if index_ltp is None:
        return abs(strike)
    return abs(strike - float(index_ltp))

def _pick_isolated(
    touched: list[dict[str, Any]],
    index_ltp_by_key: dict[str, float],
) -> dict[str, Any] | None:
    """Return the single winning contract, or None.

    Rules:
      1) If only one touched -> return it
      2) Filter by ticker priority (NIFTY > SENSEX by default)
      3) Within winning ticker: nearest strike to index LTP
    """
    if not touched:
        return None
    if len(touched) == 1:
        return dict(touched[0])

    # Sort by (priority, distance_to_index_ltp)
    def _key(c: dict[str, Any]) -> tuple[int, float]:
        prio = _ticker_priority(c.get("ticker"))
        idx_key = c.get("index_key") or ""
        index_ltp = index_ltp_by_key.get(idx_key)
        dist = _strike_distance_to_index_ltp(c, index_ltp)
        return (prio, dist)

    ordered = sorted(touched, key=_key)
    winner = dict(ordered[0])
    winner["filter_basis"] = (
        f"ticker_priority={winner.get('ticker')} "
        f"nearest_to_atm={_strike_distance_to_index_ltp(winner, index_ltp_by_key.get(winner.get('index_key') or '')):.2f}"
    )
    return winner

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class Strategy2IsolationService:
    """Strategy 2 Stage 3 — isolation-level touch filter."""

    def __init__(self) -> None:
        # in-memory: instrument_key -> touch record
        self._touches: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        self._touches.clear()

    # ------------------------------------------------------------------ #
    # Touch recording (called by the live tracker thread)                #
    # ------------------------------------------------------------------ #
    def record_touch(
        self,
        *,
        instrument_key: str,
        ticker: str,
        index_name: str,
        index_key: str,
        strike_price: float,
        strike_type: str,
        level: float,
        touch_ltp: float,
        touch_minute: str,
    ) -> bool:
        """Record the FIRST touch of a contract's isolation level.

        Returns True if this was the first touch (a new entry was created),
        False if the contract had already been touched earlier today.
        """
        if instrument_key in self._touches:
            return False

        self._touches[instrument_key] = {
            "instrument_key": instrument_key,
            "ticker": ticker,
            "index_name": index_name,
            "index_key": index_key,
            "strike_price": strike_price,
            "strike_type": strike_type,
            "isolation_level_name": _isolation_level_key(),
            "isolation_level": level,
            "touch_ltp": touch_ltp,
            "touch_minute": touch_minute,
            "touch_time": _now_iso(),
            "day": _market_now().date().isoformat(),
        }
        logger.info(
            "[strategy2] touch recorded: %s %s %s @ %s (level=%.2f, ltp=%.2f)",
            ticker,
            strike_price,
            strike_type,
            touch_minute,
            level,
            touch_ltp,
        )
        return True

    def touches(self) -> list[dict[str, Any]]:
        return list(self._touches.values())

    # ------------------------------------------------------------------ #
    # Public entry (also callable on startup / catch-up)                 #
    # ------------------------------------------------------------------ #
    def run(
        self,
        reason: str = "manual",
        *,
        mode: str | None = None,
        catchup: bool = False,
    ) -> dict[str, Any]:
        effective_mode = str(mode or reason or "manual")
        isolation_key = _isolation_level_key()
        post_market = _post_market()

        logger.info(
            "[strategy2:%s] stage_3 run (mode=%s catchup=%s post_market=%s "
            "level=%s touches=%d)",
            reason,
            effective_mode,
            catchup,
            post_market,
            isolation_key,
            len(self._touches),
        )

        if not config.STRATEGY_2_ENABLED:
            return self._summary_disabled(reason, effective_mode, catchup, post_market)

        stage2_root = _stage2_root()
        if not stage2_root.exists():
            return self._summary_no_stage2(reason, effective_mode, catchup, post_market)

        # Snapshot which contracts are Stage 2 isolated (from disk).
        stage2_contracts = self._collect_stage2_contracts(stage2_root)

        # Compute each contract's isolation level and current state.
        index_ltp_by_key: dict[str, float] = {}
        try:
            from services.live_feed_service import live_feed_service
            for key in {c["index_key"] for c in stage2_contracts if c.get("index_key")}:
                ltp = live_feed_service.get_latest_ltp(key)
                if ltp is not None:
                    index_ltp_by_key[key] = float(ltp)
        except Exception:
            logger.exception("[strategy2:%s] index LTP snapshot failed", reason)

        enriched: list[dict[str, Any]] = []
        for c in stage2_contracts:
            level, levels = _read_contract_isolation_level(
                ticker=c["ticker"],
                index_name=c["index_name"],
                strike_dir_name=c["strike_dir_name"],
            )
            c["isolation_level_name"] = isolation_key
            c["isolation_level"] = level
            c["or_levels"] = levels
            c["touched"] = c["instrument_key"] in self._touches
            if c["touched"]:
                c["touch"] = self._touches[c["instrument_key"]]
            enriched.append(c)

        touched_list = [c for c in enriched if c["touched"]]
        isolated = _pick_isolated(touched_list, index_ltp_by_key)

        # Persist per-contract touch files + summary.
        base = _stage3_root()
        base.mkdir(parents=True, exist_ok=True)

        for c in touched_list:
            out_dir = (
                base
                / _safe(c["ticker"])
                / _safe(c["index_name"])
                / c["strike_dir_name"]
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            touch = dict(c["touch"])
            touch["isolation_level"] = c.get("isolation_level")
            touch["or_levels"] = c.get("or_levels") or {}
            touch["is_isolated_pick"] = bool(
                isolated and isolated["instrument_key"] == c["instrument_key"]
            )
            _write_json(out_dir / "s3_touch.json", touch)

        summary = {
            "status": "ok",
            "stage": "stage_3",
            "strategy": "strategy_2",
            "reason": reason,
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "isolation_level_name": isolation_key,
            "generated_at": _now_iso(),
            "actual_time_ist": _market_hhmmss(),
            "stage2_contracts": len(enriched),
            "touched_contracts": len(touched_list),
            "isolated": isolated,
            "contracts": [
                {
                    "instrument_key": c["instrument_key"],
                    "ticker": c["ticker"],
                    "index_name": c["index_name"],
                    "index_key": c["index_key"],
                    "strike_price": c["strike_price"],
                    "strike_type": c["strike_type"],
                    "isolation_level": c.get("isolation_level"),
                    "touched": c["touched"],
                    "touch_minute": (
                        c["touch"]["touch_minute"] if c["touched"] else None
                    ),
                    "touch_ltp": (
                        c["touch"]["touch_ltp"] if c["touched"] else None
                    ),
                }
                for c in enriched
            ],
        }
        _write_json(base / "stage_3_summary.json", summary)

        logger.info(
            "[strategy2:%s] stage_3 COMPLETE — level=%s stage2=%d touched=%d "
            "isolated=%s",
            reason,
            isolation_key,
            len(enriched),
            len(touched_list),
            (
                f"{isolated['ticker']} {isolated['strike_price']} "
                f"{isolated['strike_type']}"
                if isolated
                else "none"
            ),
        )
        return summary

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _collect_stage2_contracts(self, stage2_root: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ticker_dir in sorted(stage2_root.iterdir()):
            if not ticker_dir.is_dir():
                continue
            for index_dir in sorted(ticker_dir.iterdir()):
                if not index_dir.is_dir():
                    continue
                for strike_dir in sorted(index_dir.iterdir()):
                    if not strike_dir.is_dir():
                        continue
                    meta = _read_json(strike_dir / "isolation_meta.json") or {}
                    s1_meta = _read_json(strike_dir / "strategy_meta.json") or {}
                    instrument_key = (
                        meta.get("upstox_instrument_key")
                        or s1_meta.get("upstox_instrument_key")
                        or meta.get("instrument_key")
                        or s1_meta.get("instrument_key")
                    )
                    if not instrument_key:
                        continue
                    out.append({
                        "instrument_key": str(instrument_key),
                        "ticker": str(meta.get("ticker") or ticker_dir.name),
                        "index_name": str(
                            meta.get("index_name") or index_dir.name
                        ),
                        "index_key": str(
                            s1_meta.get("index_instrument_key") or ""
                        ),
                        "strike_price": meta.get(
                            "strike_price", s1_meta.get("strike_price")
                        ),
                        "strike_type": meta.get(
                            "strike_type", s1_meta.get("strike_type")
                        ),
                        "strike_dir_name": strike_dir.name,
                    })
        return out

    def _summary_disabled(
        self, reason: str, mode: str, catchup: bool, post_market: bool
    ) -> dict[str, Any]:
        return {
            "status": "disabled",
            "stage": "stage_3",
            "strategy": "strategy_2",
            "reason": reason,
            "mode": mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "isolation_level_name": _isolation_level_key(),
            "generated_at": _now_iso(),
            "stage2_contracts": 0,
            "touched_contracts": 0,
            "isolated": None,
            "contracts": [],
        }

    def _summary_no_stage2(
        self, reason: str, mode: str, catchup: bool, post_market: bool
    ) -> dict[str, Any]:
        return {
            "status": "no_stage_2",
            "stage": "stage_3",
            "strategy": "strategy_2",
            "reason": reason,
            "mode": mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "isolation_level_name": _isolation_level_key(),
            "generated_at": _now_iso(),
            "stage2_contracts": 0,
            "touched_contracts": 0,
            "isolated": None,
            "contracts": [],
        }

strategy_2_isolation_service = Strategy2IsolationService()