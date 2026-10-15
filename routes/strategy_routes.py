"""Strategy API — stage 1 / 2 / 3 / 4.

Stage 1 (buffer filter)      data/strategy/stage_1_initial/
Stage 2 (EMA isolation)      data/strategy/stage_2_isolation/
Stage 3 (R2 activation)      data/strategy/stage_3_r2_activated/
Stage 4 (order placement)    data/strategy/stage_4_order/

Endpoints:

    # Stage 1
    GET  /admin/strategy
    GET  /admin/strategy/indexes
    GET  /admin/strategy/{ticker}
    GET  /admin/strategy/{ticker}/strikes
    POST /admin/strategy/recalculate

    # Stage 2
    GET  /admin/strategy/isolation
    GET  /admin/strategy/isolation/indexes
    GET  /admin/strategy/isolation/{ticker}
    GET  /admin/strategy/isolation/{ticker}/strikes
    POST /admin/strategy/isolation/recalculate

    # Stage 3 (tracker)
    GET  /admin/strategy/tracker/status
    POST /admin/strategy/tracker/start
    POST /admin/strategy/tracker/stop
    GET  /admin/strategy/stage3
    GET  /admin/strategy/stage3/{ticker}

    # Stage 4 (orders)
    GET  /admin/strategy/stage4
    GET  /admin/strategy/stage4/{ticker}
    POST /admin/strategy/order/run
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from core import config
from services.normalization import normalize_underlying
from services.strategy_isolation_service import strategy_isolation_service
from services.strategy_live_tracker_service import (
    strategy_live_tracker_service,
)
from services.strategy_order_service import strategy_order_service
from services.strategy_selection_service import strategy_selection_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/strategy", tags=["strategy"])


# ---------------------------------------------------------------------------
# Paths / helpers
# ---------------------------------------------------------------------------
def _base() -> Path:
    return Path(config.STRATEGY_BASE_DIR)


def _stage1_root() -> Path:
    return _base() / config.STRATEGY_STAGE_1_NAME


def _stage2_root() -> Path:
    return _base() / config.STRATEGY_STAGE_2_NAME


def _stage3_root() -> Path:
    return _base() / config.STRATEGY_STAGE_3_NAME


def _stage4_root() -> Path:
    return _base() / config.STRATEGY_STAGE_4_NAME


def _safe(name: str) -> str:
    text = str(name or "").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ticker_dirs(root: Path, ticker: str) -> list[Path]:
    candidates = []
    for text in (ticker, normalize_underlying(ticker)):
        if not text:
            continue
        safe = _safe(text)
        if safe:
            candidates.append(root / safe)
    seen: set[str] = set()
    out: list[Path] = []
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        if c.is_dir():
            out.append(c)
    return out


def _list_stage(root: Path, summary_filename: str) -> dict[str, Any]:
    if not root.exists():
        return {"count": 0, "indexes": []}
    items: list[dict[str, Any]] = []
    for ticker_dir in sorted(root.iterdir()):
        if not ticker_dir.is_dir():
            continue
        for index_dir in sorted(ticker_dir.iterdir()):
            if not index_dir.is_dir():
                continue
            summary = _read(index_dir / summary_filename)
            if summary:
                items.append(summary)
    return {"count": len(items), "indexes": items}


def _read_ticker(
    root: Path, ticker: str, summary_filename: str,
) -> dict[str, Any] | None:
    for ticker_dir in _ticker_dirs(root, ticker):
        for index_dir in sorted(ticker_dir.iterdir()):
            if not index_dir.is_dir():
                continue
            summary = _read(index_dir / summary_filename)
            if summary:
                return summary
    return None


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------
@router.get("")
def read_summary() -> dict[str, Any]:
    data = _read(_stage1_root() / "stage_1_summary.json")
    if not data:
        data = _read(_base() / "strategy_summary.json")
    if not data:
        return {"status": "not_generated", "base_dir": str(_base())}
    return data


@router.get("/indexes")
def list_indexes() -> dict[str, Any]:
    return _list_stage(_stage1_root(), "stage_1_summary.json")


@router.get("/{ticker}")
def read_index(ticker: str) -> dict[str, Any]:
    data = _read_ticker(_stage1_root(), ticker, "stage_1_summary.json")
    if data is None:
        raise HTTPException(status_code=404, detail="Ticker not found")
    return data


@router.get("/{ticker}/strikes")
def read_strikes(ticker: str) -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    for ticker_dir in _ticker_dirs(_stage1_root(), ticker):
        for index_dir in sorted(ticker_dir.iterdir()):
            if not index_dir.is_dir():
                continue
            for strike_dir in sorted(index_dir.iterdir()):
                if not strike_dir.is_dir():
                    continue
                meta = _read(strike_dir / "strategy_meta.json") or {}
                last = _read(strike_dir / "last_day_cross.json") or {}
                contracts.append(
                    {
                        "strike_price": meta.get("strike_price"),
                        "strike_type": meta.get("strike_type"),
                        "atm_strike": meta.get("atm_strike"),
                        "atm_reference": meta.get("atm_reference"),
                        "buffer_points": meta.get("buffer_points"),
                        "upstox_instrument_key":
                            meta.get("upstox_instrument_key"),
                        "placeholder_instrument_key":
                            meta.get("placeholder_instrument_key"),
                        "last_day_cross_type": last.get("type"),
                        "folder": str(strike_dir),
                    }
                )
        if contracts:
            break
    return {
        "ticker": normalize_underlying(ticker) or ticker,
        "contract_count": len(contracts),
        "contracts": contracts,
    }


@router.post("/recalculate")
async def recalculate() -> dict[str, Any]:
    return await asyncio.to_thread(strategy_selection_service.run, "manual")


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------
@router.get("/isolation")
def read_isolation_summary() -> dict[str, Any]:
    data = _read(_stage2_root() / "strategy_isolation.json")
    if not data:
        return {"status": "not_generated", "base_dir": str(_stage2_root())}
    return data


@router.get("/isolation/indexes")
def list_isolation_indexes() -> dict[str, Any]:
    return _list_stage(_stage2_root(), "stage_2_summary.json")


@router.get("/isolation/{ticker}")
def read_isolation_index(ticker: str) -> dict[str, Any]:
    data = _read_ticker(_stage2_root(), ticker, "stage_2_summary.json")
    if data is None:
        raise HTTPException(status_code=404, detail="Ticker not found")
    return data


@router.get("/isolation/{ticker}/strikes")
def read_isolation_strikes(ticker: str) -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    for ticker_dir in _ticker_dirs(_stage2_root(), ticker):
        for index_dir in sorted(ticker_dir.iterdir()):
            if not index_dir.is_dir():
                continue
            for strike_dir in sorted(index_dir.iterdir()):
                if not strike_dir.is_dir():
                    continue
                meta = _read(strike_dir / "isolation_meta.json")
                if not meta:
                    continue
                contracts.append(
                    {
                        "strike_price": meta.get("strike_price"),
                        "strike_type": meta.get("strike_type"),
                        "upstox_instrument_key":
                            meta.get("upstox_instrument_key"),
                        "last_day_type": meta.get("last_day_type"),
                        "today_type": meta.get("today_type"),
                        "decision": meta.get("decision"),
                        "paired_trigger": meta.get("paired_trigger"),
                        "reason": meta.get("reason"),
                        "folder": str(strike_dir),
                    }
                )
        if contracts:
            break
    return {
        "ticker": normalize_underlying(ticker) or ticker,
        "isolated_count": len(contracts),
        "contracts": contracts,
    }


@router.post("/isolation/recalculate")
async def recalculate_isolation() -> dict[str, Any]:
    return await asyncio.to_thread(strategy_isolation_service.run, "manual")


# ---------------------------------------------------------------------------
# Stage 3 (tracker)
# ---------------------------------------------------------------------------
@router.get("/tracker/status")
def tracker_status() -> dict[str, Any]:
    return strategy_live_tracker_service.status()


@router.post("/tracker/start")
def tracker_start() -> dict[str, Any]:
    ok = strategy_live_tracker_service.start()
    return {"started": ok, "status": strategy_live_tracker_service.status()}


@router.post("/tracker/stop")
def tracker_stop() -> dict[str, Any]:
    strategy_live_tracker_service.stop()
    return {"stopped": True, "status": strategy_live_tracker_service.status()}


@router.get("/stage3")
def read_stage3_summary() -> dict[str, Any]:
    data = _read(_stage3_root() / "stage_3_summary.json")
    if not data:
        return {"status": "not_generated", "base_dir": str(_stage3_root())}
    return data


@router.get("/stage3/{ticker}")
def read_stage3_ticker(ticker: str) -> dict[str, Any]:
    ticker_dir = _stage3_root() / _safe(normalize_underlying(ticker) or ticker)
    if not ticker_dir.is_dir():
        raise HTTPException(status_code=404, detail="Ticker not found")

    items: list[dict[str, Any]] = []
    for index_dir in sorted(ticker_dir.iterdir()):
        if not index_dir.is_dir():
            continue
        for strike_dir in sorted(index_dir.iterdir()):
            if not strike_dir.is_dir():
                continue
            activation = _read(strike_dir / "r2_activation.json")
            if activation:
                items.append(activation)

    return {
        "ticker": normalize_underlying(ticker) or ticker,
        "activated_count": len(items),
        "activated": items,
    }


# ---------------------------------------------------------------------------
# Stage 4 (orders)
# ---------------------------------------------------------------------------
@router.get("/stage4")
def read_stage4_summary() -> dict[str, Any]:
    data = _read(_stage4_root() / "stage_4_summary.json")
    if not data:
        return {"status": "not_generated", "base_dir": str(_stage4_root())}
    return data


@router.get("/stage4/{ticker}")
def read_stage4_ticker(ticker: str) -> dict[str, Any]:
    ticker_dir = _stage4_root() / _safe(normalize_underlying(ticker) or ticker)
    if not ticker_dir.is_dir():
        raise HTTPException(status_code=404, detail="Ticker not found")

    items: list[dict[str, Any]] = []
    for index_dir in sorted(ticker_dir.iterdir()):
        if not index_dir.is_dir():
            continue
        orders = _read(index_dir / "orders.json")
        if orders:
            items.append(orders)

    return {
        "ticker": normalize_underlying(ticker) or ticker,
        "index_count": len(items),
        "orders_by_index": items,
    }


@router.post("/order/run")
async def run_order() -> dict[str, Any]:
    return await asyncio.to_thread(strategy_order_service.run, "manual")