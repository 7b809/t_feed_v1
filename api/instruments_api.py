"""
api/instruments_api.py

Instrument lookup + candle retrieval + crossovers + full refresh job.

Lookup modes accepted by every instrument endpoint:
  A) ?instrument_key=NSE_FO|12345
  B) ?strike=23000&option_type=CE
  C) ?index=NIFTY&strike=23000&option_type=CE[&expiry=YYYY-MM-DD]

Endpoints:
  GET  /api/instruments/status
  GET  /api/instruments/contract
  GET  /api/instruments/candles/historical
  GET  /api/instruments/candles/intraday
  GET  /api/instruments/candles              (historical + intraday)
  GET  /api/instruments/crossovers/historic
  GET  /api/instruments/crossovers/intraday
  POST /api/instruments/refresh              (full orchestrated job)
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import upstox_client
from fastapi import APIRouter, HTTPException, Query

from core.config import core_config
from core.logger import get_logger
from upstox_app.candle.candle_storage import candle_storage
from upstox_app.candle.crossover_service import crossover_service
from upstox_app.candle.crossover_storage import crossover_storage
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


# ---------------------------------------------------------------------------
# Contract resolution helpers
# ---------------------------------------------------------------------------


def _enabled_indexes() -> List[str]:
    return [
        name.upper()
        for name, meta in core_config.MAIN_INDEXES.items()
        if meta.get("enabled")
    ]


def _load_contracts_from_service(index_name: str) -> List[Dict[str, Any]]:
    try:
        from upstox_app.option.option_service import option_service

        getter = getattr(option_service, "get_contracts", None)
        if callable(getter):
            return list(getter(index_name) or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("option_service lookup unavailable | err=%s", exc)
    return []


def _load_contracts_from_disk(index_name: str) -> List[Dict[str, Any]]:
    path = (
        Path(upstox_config.OPTIONS_RUNTIME_DIR)
        / "options"
        / f"{index_name.upper()}.json"
    )
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return list(json.load(fh).get("contracts") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Runtime snapshot read failed | index=%s | err=%s", index_name, exc
        )
        return []


def _contracts_for_index(index_name: str) -> List[Dict[str, Any]]:
    contracts = _load_contracts_from_service(index_name)
    return contracts if contracts else _load_contracts_from_disk(index_name)


def _close(a: Any, b: float, tol: float = 1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) < tol
    except (TypeError, ValueError):
        return False


def _find_by_instrument_key(
    instrument_key: str,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    for index_name in _enabled_indexes():
        for c in _contracts_for_index(index_name):
            if c.get("instrument_key") == instrument_key:
                return index_name, c
    return None


def _find_by_strike(
    strike: float,
    option_type: str,
    index_name: Optional[str] = None,
    expiry: Optional[str] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    candidates = [index_name.upper()] if index_name else _enabled_indexes()
    otype = option_type.upper()
    for ix in candidates:
        matches = [
            c
            for c in _contracts_for_index(ix)
            if (c.get("option_type") or "").upper() == otype
            and _close(c.get("strike_price"), strike)
        ]
        if expiry:
            matches = [c for c in matches if c.get("expiry") == expiry]
        if matches:
            matches.sort(key=lambda c: c.get("expiry") or "")
            return ix, matches[0]
    return None


def _resolve_contract(
    instrument_key: Optional[str],
    index: Optional[str],
    strike: Optional[float],
    option_type: Optional[str],
    expiry: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    if instrument_key:
        found = _find_by_instrument_key(instrument_key)
        if not found:
            raise HTTPException(
                status_code=404, detail=f"instrument_key not found: {instrument_key}"
            )
        return found
    if strike is not None and option_type:
        found = _find_by_strike(strike, option_type, index, expiry)
        if not found:
            raise HTTPException(
                status_code=404,
                detail=f"contract not found | index={index or '*'} strike={strike} "
                f"type={option_type} expiry={expiry}",
            )
        return found
    raise HTTPException(
        status_code=400,
        detail="provide instrument_key OR (strike + option_type), optionally with index/expiry",
    )


# ---------------------------------------------------------------------------
# Upstream candle helpers (no access token needed for HistoryV3Api)
# ---------------------------------------------------------------------------


def _new_api() -> "upstox_client.HistoryV3Api":
    return upstox_client.HistoryV3Api()


def _extract_candles(response: Any) -> List[List[Any]]:
    if response is None:
        return []
    data = (
        response.get("data")
        if isinstance(response, dict)
        else getattr(response, "data", None)
    )
    if not data:
        return []
    candles = (
        data.get("candles")
        if isinstance(data, dict)
        else getattr(data, "candles", None)
    )
    return list(candles or [])


def _fetch_historical(instrument_key, unit, interval, to_date, from_date):
    return _extract_candles(
        _new_api().get_historical_candle_data1(
            instrument_key, unit, interval, to_date, from_date
        )
    )


def _fetch_intraday(instrument_key, unit, interval):
    return _extract_candles(
        _new_api().get_intra_day_candle_data(instrument_key, unit, interval)
    )


# ---------------------------------------------------------------------------
# Readonly candle helpers
# ---------------------------------------------------------------------------


def _read_stored(index_name: str, contract: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    strike = contract.get("strike_price")
    otype = contract.get("option_type") or contract.get("instrument_type")
    if strike is None or not otype:
        return None
    return candle_storage.load(index_name, strike, otype)


def _slice_by_date(
    candles: List[List[Any]], from_date: str, to_date: str
) -> List[List[Any]]:
    out: List[List[Any]] = []
    for row in candles:
        if not row:
            continue
        day = str(row[0])[:10]
        if from_date <= day <= to_date:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Status / contract
# ---------------------------------------------------------------------------


@router.get("/status")
def status() -> Dict[str, Any]:
    return {
        "enabled_indexes": _enabled_indexes(),
        "readonly_root": str(upstox_config.OPTIONS_READONLY_DIR),
        "runtime_root": str(upstox_config.OPTIONS_RUNTIME_DIR),
        "default_historical_days": upstox_config.CANDLES_DAYS,
        "default_unit": upstox_config.CANDLES_UNIT,
        "default_interval": upstox_config.CANDLES_INTERVAL,
        "crossover_enabled": bool(getattr(upstox_config, "CROSSOVER_ENABLED", True)),
        "crossover_ema_fast": int(getattr(upstox_config, "CROSSOVER_EMA_FAST", 9)),
        "crossover_ema_slow": int(getattr(upstox_config, "CROSSOVER_EMA_SLOW", 21)),
    }


@router.get("/contract")
def get_contract(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
) -> Dict[str, Any]:
    index_name, contract = _resolve_contract(
        instrument_key, index, strike, option_type, expiry
    )
    return {
        "found": True,
        "index_name": index_name,
        "instrument_key": contract.get("instrument_key"),
        "contract": contract,
    }


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------


@router.get("/candles/historical")
def get_historical(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    unit: str = Query("minutes"),
    interval: str = Query("1"),
    source: str = Query("auto", pattern="^(auto|readonly|upstream)$"),
) -> Dict[str, Any]:
    index_name, contract = _resolve_contract(
        instrument_key, index, strike, option_type, expiry
    )
    key = contract.get("instrument_key")

    today = date.today()
    yesterday = today - timedelta(days=1)
    to_d = to_date or yesterday.isoformat()
    from_d = from_date or (today - timedelta(days=7)).isoformat()

    if source in ("auto", "readonly"):
        stored = _read_stored(index_name, contract)
        if stored:
            sliced = _slice_by_date(stored.get("candles", []), from_d, to_d)
            return {
                "found": True,
                "instrument_key": key,
                "index_name": index_name,
                "source": "readonly",
                "unit": stored.get("unit", unit),
                "interval": stored.get("interval", interval),
                "from_date": from_d,
                "to_date": to_d,
                "count": len(sliced),
                "candles": sliced,
            }
        if source == "readonly":
            raise HTTPException(status_code=404, detail="no readonly candle file")

    try:
        candles = _fetch_historical(key, unit, interval, to_d, from_d)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Historical fetch failed | key=%s | err=%s", key, exc)
        raise HTTPException(
            status_code=502, detail=f"upstream historical fetch failed: {exc}"
        )

    return {
        "found": True,
        "instrument_key": key,
        "index_name": index_name,
        "source": "upstream",
        "unit": unit,
        "interval": interval,
        "from_date": from_d,
        "to_date": to_d,
        "count": len(candles),
        "candles": candles,
    }


@router.get("/candles/intraday")
def get_intraday(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    unit: str = Query("minutes"),
    interval: str = Query("1"),
) -> Dict[str, Any]:
    index_name, contract = _resolve_contract(
        instrument_key, index, strike, option_type, expiry
    )
    key = contract.get("instrument_key")
    try:
        candles = _fetch_intraday(key, unit, interval)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Intraday fetch failed | key=%s | err=%s", key, exc)
        raise HTTPException(
            status_code=502, detail=f"upstream intraday fetch failed: {exc}"
        )
    today_iso = date.today().isoformat()
    return {
        "found": True,
        "instrument_key": key,
        "index_name": index_name,
        "source": "upstream",
        "unit": unit,
        "interval": interval,
        "from_date": today_iso,
        "to_date": today_iso,
        "count": len(candles),
        "candles": candles,
    }


@router.get("/candles")
def get_candles(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    unit: str = Query("minutes"),
    interval: str = Query("1"),
    source: str = Query("auto", pattern="^(auto|readonly|upstream)$"),
) -> Dict[str, Any]:
    historical = get_historical(
        instrument_key,
        index,
        strike,
        option_type,
        expiry,
        from_date,
        to_date,
        unit,
        interval,
        source,
    )
    intraday = get_intraday(
        instrument_key, index, strike, option_type, expiry, unit, interval
    )
    return {
        "found": True,
        "instrument_key": historical["instrument_key"],
        "index_name": historical["index_name"],
        "unit": unit,
        "interval": interval,
        "historical": historical,
        "intraday": intraday,
    }


# ---------------------------------------------------------------------------
# Crossovers
# ---------------------------------------------------------------------------


@router.get("/crossovers/historic")
def get_historic_crossovers(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
) -> Dict[str, Any]:
    index_name, contract = _resolve_contract(
        instrument_key, index, strike, option_type, expiry
    )
    strike_v = contract.get("strike_price")
    otype = contract.get("option_type") or contract.get("instrument_type")
    data = crossover_storage.load(index_name, strike_v, otype, "historic")
    if data is None:
        raise HTTPException(status_code=404, detail="no historic crossover file")
    return data


@router.get("/crossovers/intraday")
def get_intraday_crossovers(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
) -> Dict[str, Any]:
    index_name, contract = _resolve_contract(
        instrument_key, index, strike, option_type, expiry
    )
    strike_v = contract.get("strike_price")
    otype = contract.get("option_type") or contract.get("instrument_type")
    data = crossover_storage.load(index_name, strike_v, otype, "intraday")
    if data is None:
        raise HTTPException(status_code=404, detail="no intraday crossover file")
    return data


# ---------------------------------------------------------------------------
# Full orchestrated refresh
# ---------------------------------------------------------------------------


@router.post("/refresh")
async def refresh_all() -> Dict[str, Any]:
    """
    Full orchestrated refresh job. Runs sequentially:

      1. Reload token from MongoDB into the local cache.
      2. Validate the cached access token via the Upstox Profile API.
      3. Ensure every enabled index is subscribed on the market streamer.
      4. Ensure option contracts are present for every enabled index.
      5. Ensure candles are present for every contract (fetch missing).
      6. Recompute 9/21 EMA crossovers (historic + intraday) and save them.
    """
    import asyncio
    from datetime import datetime

    summary: Dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "steps": {},
        "finished_at": None,
    }

    # Step 1: token refresh
    try:
        from token_tasks.service import token_service

        await asyncio.to_thread(token_service.load_token)
        summary["steps"]["token_refresh"] = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh | token refresh failed: %s", exc)
        summary["steps"]["token_refresh"] = {"status": "error", "error": str(exc)}

    # Step 2: token validation
    try:
        from token_tasks.validator import validate_token  # type: ignore

        result = await asyncio.to_thread(validate_token)
        summary["steps"]["token_validate"] = {"status": "ok", "valid": bool(result)}
    except ImportError:
        # Fall back to profile check
        try:
            from upstox_app.profile.get_profile_status import get_profile_status  # type: ignore

            result = await asyncio.to_thread(get_profile_status)
            summary["steps"]["token_validate"] = {"status": "ok", "profile": result}
        except Exception as exc:  # noqa: BLE001
            summary["steps"]["token_validate"] = {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh | token validate failed: %s", exc)
        summary["steps"]["token_validate"] = {"status": "error", "error": str(exc)}

    # Step 3: index subscriptions
    try:
        from upstox_app.streamer.streamer_manager import subscribe_enabled_indexes

        res = await asyncio.to_thread(subscribe_enabled_indexes)
        summary["steps"]["index_subscription"] = {"status": "ok", "result": res}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh | index subscription failed: %s", exc)
        summary["steps"]["index_subscription"] = {"status": "error", "error": str(exc)}

    # Step 4: ensure contracts
    try:
        from upstox_app.option.option_service import load_enabled_indexes
        from api.instruments_api import _contracts_for_index  # noqa: PLC0415

        missing = []
        for name in _enabled_indexes():
            if not _contracts_for_index(name):
                missing.append(name)
        if missing:
            res = await asyncio.to_thread(load_enabled_indexes)
            summary["steps"]["contracts"] = {
                "status": "reloaded",
                "missing": missing,
                "result": res,
            }
        else:
            summary["steps"]["contracts"] = {"status": "present"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh | contracts step failed: %s", exc)
        summary["steps"]["contracts"] = {"status": "error", "error": str(exc)}

    # Step 5: candles
    try:
        from upstox_app.candle.candle_service import candle_service

        res = await asyncio.to_thread(candle_service.ensure_all_enabled)
        summary["steps"]["candles"] = {"status": "ok", "totals": res.get("totals", {})}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh | candle step failed: %s", exc)
        summary["steps"]["candles"] = {"status": "error", "error": str(exc)}

    # Step 6: crossovers
    try:
        res = await asyncio.to_thread(crossover_service.calculate_all_enabled)
        summary["steps"]["crossovers"] = {
            "status": "ok",
            "totals": res.get("totals", {}),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh | crossover step failed: %s", exc)
        summary["steps"]["crossovers"] = {"status": "error", "error": str(exc)}

    summary["finished_at"] = datetime.now().astimezone().isoformat()
    return summary
