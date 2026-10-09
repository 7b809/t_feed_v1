"""Read APIs for candles and EMA crosses.

Two addressing styles are supported everywhere:

1. By ``instrument_key`` (exact match, e.g. ``NSE_FO|44694`` or
   ``NSE_INDEX|Nifty 50``). The subscription snapshot is consulted to
   recover the on-disk path for options and futures.
2. By parts: ``underlying`` (required) plus optional ``strike`` and
   ``type`` (``CE``/``PE``/``FUT``).

Endpoints
---------
GET /api/candles
    Query the rolling candle file. Defaults to the last 7 days.
GET /api/ema-crosses
    Query the EMA cross file. Defaults to the last 7 days.

Both endpoints accept ``days`` (default 7) and ``limit`` (optional cap on
the number of returned rows, applied after the date filter).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.logger import get_logger
from app.services.instrument_paths import (
    build_candle_path,
    build_dir_from_instrument_key,
    build_dir_from_parts,
    build_ema_cross_path,
)
from app.services.subscription_store import subscription_store

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["candles"])

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_DAYS = 7


# ---------------------------------------------------------------------------
# Instrument resolution
# ---------------------------------------------------------------------------
def _resolve_dir(
    instrument_key: str | None,
    underlying: str | None,
    strike: Any | None,
    strike_type: str | None,
) -> tuple[Path, dict[str, Any]]:
    """Return (instrument_dir, instrument_info).

    Preference order:
    1. ``instrument_key`` matched against the in-memory subscription snapshot.
    2. ``instrument_key`` that is an index row.
    3. ``underlying`` + optional ``strike`` + ``type``.
    """
    if instrument_key:
        instruments = subscription_store.snapshot().get("active", [])
        match = next(
            (
                i
                for i in instruments
                if isinstance(i, dict)
                and i.get("instrument_key") == instrument_key
            ),
            None,
        )
        if match is not None:
            # Use the same layout the jobs use.
            from app.services.instrument_paths import _instrument_dir  # type: ignore

            return _instrument_dir(match), match

        index_dir = build_dir_from_instrument_key(instrument_key)
        if index_dir is not None:
            return index_dir, {"instrument_key": instrument_key}

        raise HTTPException(
            status_code=404,
            detail=(
                f"instrument_key '{instrument_key}' not found in the current "
                "subscription snapshot and is not an index row"
            ),
        )

    if underlying:
        instrument_dir = build_dir_from_parts(underlying, strike, strike_type)
        return instrument_dir, {
            "underlying": underlying,
            "strike": strike,
            "type": strike_type,
        }

    raise HTTPException(
        status_code=422,
        detail="Provide either instrument_key or underlying",
    )


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Unable to read JSON at %s", path)
        return None


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------
@router.get("/candles")
async def get_candles(
    instrument_key: str | None = Query(
        default=None,
        description="Exact instrument key, e.g. NSE_FO|44694 or NSE_INDEX|Nifty 50",
    ),
    underlying: str | None = Query(
        default=None,
        description="Underlying name, e.g. nifty or sensex",
    ),
    strike: str | None = Query(
        default=None,
        description="Strike value, e.g. 24450 (options only)",
    ),
    type: str | None = Query(
        default=None,
        description="Instrument type: CE, PE, or FUT (optional)",
    ),
    days: int = Query(
        default=DEFAULT_DAYS,
        ge=1,
        le=90,
        description="Rolling window in days (default 7)",
    ),
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Optional cap on number of returned candles",
    ),
) -> dict[str, Any]:
    """Return candles for an instrument.

    Addressed by ``instrument_key`` or by ``underlying`` (+ optional
    ``strike``/``type``). Defaults to the last 7 days.
    """
    instrument_dir, info = _resolve_dir(instrument_key, underlying, strike, type)
    candle_path = instrument_dir / "historical.json"

    payload = _read_json(candle_path)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"No candle file found at {candle_path}",
        )

    raw_candles = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(raw_candles, list):
        raise HTTPException(
            status_code=500,
            detail=f"Malformed candle file at {candle_path}",
        )

    cutoff = date.today() - timedelta(days=days - 1)
    filtered: list[dict[str, Any]] = []
    for candle in raw_candles:
        if not isinstance(candle, dict):
            continue
        day = _parse_date(candle.get("timestamp"))
        if day is None or day < cutoff:
            continue
        filtered.append(candle)

    if limit is not None:
        filtered = filtered[-limit:]

    return {
        "instrument": info,
        "path": str(candle_path),
        "days": days,
        "cutoff": cutoff.isoformat(),
        "count": len(filtered),
        "candles": filtered,
        "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
    }


# ---------------------------------------------------------------------------
# EMA crosses
# ---------------------------------------------------------------------------
@router.get("/ema-crosses")
async def get_ema_crosses(
    instrument_key: str | None = Query(
        default=None,
        description="Exact instrument key, e.g. NSE_FO|44694 or NSE_INDEX|Nifty 50",
    ),
    underlying: str | None = Query(
        default=None,
        description="Underlying name, e.g. nifty or sensex",
    ),
    strike: str | None = Query(
        default=None,
        description="Strike value, e.g. 24450 (options only)",
    ),
    type: str | None = Query(
        default=None,
        description="Instrument type: CE, PE, or FUT (optional)",
    ),
    days: int = Query(
        default=DEFAULT_DAYS,
        ge=1,
        le=90,
        description="Rolling window in days (default 7)",
    ),
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Optional cap on number of returned crosses",
    ),
) -> dict[str, Any]:
    """Return EMA crosses for an instrument.

    Addressed by ``instrument_key`` or by ``underlying`` (+ optional
    ``strike``/``type``). Defaults to the last 7 days.
    """
    instrument_dir, info = _resolve_dir(instrument_key, underlying, strike, type)
    cross_path = instrument_dir / "ema_crosses.json"

    payload = _read_json(cross_path)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"No EMA cross file found at {cross_path}",
        )

    raw_crosses = payload.get("crosses") if isinstance(payload, dict) else None
    if not isinstance(raw_crosses, list):
        raise HTTPException(
            status_code=500,
            detail=f"Malformed EMA cross file at {cross_path}",
        )

    cutoff = date.today() - timedelta(days=days - 1)
    filtered: list[dict[str, Any]] = []
    for cross in raw_crosses:
        if not isinstance(cross, dict):
            continue
        day = _parse_date(cross.get("timestamp"))
        if day is None or day < cutoff:
            continue
        filtered.append(cross)

    if limit is not None:
        filtered = filtered[-limit:]

    return {
        "instrument": info,
        "path": str(cross_path),
        "days": days,
        "cutoff": cutoff.isoformat(),
        "fast_period": payload.get("fast_period") if isinstance(payload, dict) else None,
        "slow_period": payload.get("slow_period") if isinstance(payload, dict) else None,
        "count": len(filtered),
        "crosses": filtered,
        "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
    }