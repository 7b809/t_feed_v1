"""
api/instruments_api.py

Instrument lookup + candle retrieval.

Lookup modes accepted by every endpoint:
  A) ?instrument_key=NSE_FO|12345
  B) ?strike=23000&option_type=CE            (searches all enabled indexes)
  C) ?index=NIFTY&strike=23000&option_type=CE[&expiry=YYYY-MM-DD]

Endpoints:
  GET /api/instruments/status
  GET /api/instruments/contract
  GET /api/instruments/candles/historical
  GET /api/instruments/candles/intraday
  GET /api/instruments/candles              (historical + intraday)

Historical default window: today-7 → yesterday.
Historical never returns today; use intraday for today.
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
from upstox_app.common.config import upstox_config
from api.schemas import (
    CandleResponse,
    CombinedCandleResponse,
    ContractLookupResponse,
    InstrumentsStatus,
)

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
    path = Path(core_config.OPTIONS_RUNTIME_DIR) / "options" / f"{index_name.upper()}.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return list(json.load(fh).get("contracts") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Runtime snapshot read failed | index=%s | err=%s", index_name, exc)
        return []


def _contracts_for_index(index_name: str) -> List[Dict[str, Any]]:
    contracts = _load_contracts_from_service(index_name)
    if contracts:
        return contracts
    return _load_contracts_from_disk(index_name)


def _close(a: Any, b: float, tol: float = 1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) < tol
    except (TypeError, ValueError):
        return False


def _find_by_instrument_key(instrument_key: str) -> Optional[Tuple[str, Dict[str, Any]]]:
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
            c for c in _contracts_for_index(ix)
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
            raise HTTPException(status_code=404, detail=f"instrument_key not found: {instrument_key}")
        return found

    if strike is not None and option_type:
        found = _find_by_strike(strike, option_type, index, expiry)
        if not found:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"contract not found | index={index or '*'} strike={strike} "
                    f"type={option_type} expiry={expiry}"
                ),
            )
        return found

    raise HTTPException(
        status_code=400,
        detail="provide instrument_key OR (strike + option_type), optionally with index/expiry",
    )


# ---------------------------------------------------------------------------
# Upstream candle helpers (no access token for HistoryV3Api)
# ---------------------------------------------------------------------------


def _new_api() -> "upstox_client.HistoryV3Api":
    return upstox_client.HistoryV3Api()


def _extract_candles(response: Any) -> List[List[Any]]:
    if response is None:
        return []
    data = response.get("data") if isinstance(response, dict) else getattr(response, "data", None)
    if not data:
        return []
    candles = data.get("candles") if isinstance(data, dict) else getattr(data, "candles", None)
    return list(candles or [])


def _fetch_historical(instrument_key: str, unit: str, interval: str,
                      to_date: str, from_date: str) -> List[List[Any]]:
    api = _new_api()
    resp = api.get_historical_candle_data1(instrument_key, unit, interval, to_date, from_date)
    return _extract_candles(resp)


def _fetch_intraday(instrument_key: str, unit: str, interval: str) -> List[List[Any]]:
    api = _new_api()
    resp = api.get_intra_day_candle_data(instrument_key, unit, interval)
    return _extract_candles(resp)


# ---------------------------------------------------------------------------
# Readonly helpers
# ---------------------------------------------------------------------------


def _read_stored(index_name: str, contract: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    strike = contract.get("strike_price")
    otype = contract.get("option_type") or contract.get("instrument_type")
    if strike is None or not otype:
        return None
    return candle_storage.load(index_name, strike, otype)


def _slice_by_date(candles: List[List[Any]], from_date: str, to_date: str) -> List[List[Any]]:
    out: List[List[Any]] = []
    for row in candles:
        if not row:
            continue
        day = str(row[0])[:10]
        if from_date <= day <= to_date:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", response_model=InstrumentsStatus)
def status() -> InstrumentsStatus:
    return InstrumentsStatus(
        enabled_indexes=_enabled_indexes(),
        readonly_root=str(core_config.OPTIONS_READONLY_DIR),
        runtime_root=str(core_config.OPTIONS_RUNTIME_DIR),
        default_historical_days=upstox_config.CANDLES_DAYS,
        default_unit=upstox_config.CANDLES_UNIT,
        default_interval=upstox_config.CANDLES_INTERVAL,
    )


@router.get("/contract", response_model=ContractLookupResponse)
def get_contract(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
) -> ContractLookupResponse:
    index_name, contract = _resolve_contract(instrument_key, index, strike, option_type, expiry)
    return ContractLookupResponse(
        found=True,
        index_name=index_name,
        instrument_key=contract.get("instrument_key"),
        contract=contract,
    )


@router.get("/candles/historical", response_model=CandleResponse)
def get_historical(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD, default today-7"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD, default yesterday"),
    unit: str = Query("minutes"),
    interval: str = Query("1"),
    source: str = Query("auto", pattern="^(auto|readonly|upstream)$"),
) -> CandleResponse:
    index_name, contract = _resolve_contract(instrument_key, index, strike, option_type, expiry)
    key = contract.get("instrument_key")

    today = date.today()
    yesterday = today - timedelta(days=1)
    to_d = to_date or yesterday.isoformat()
    from_d = from_date or (today - timedelta(days=7)).isoformat()

    # Fast path: readonly snapshot (only when the caller accepts it)
    if source in ("auto", "readonly"):
        stored = _read_stored(index_name, contract)
        if stored:
            sliced = _slice_by_date(stored.get("candles", []), from_d, to_d)
            return CandleResponse(
                found=True,
                instrument_key=key,
                index_name=index_name,
                source="readonly",
                unit=stored.get("unit", unit),
                interval=stored.get("interval", interval),
                from_date=from_d,
                to_date=to_d,
                count=len(sliced),
                candles=sliced,
            )
        if source == "readonly":
            raise HTTPException(
                status_code=404,
                detail=f"no readonly candle file for index={index_name} strike={contract.get('strike_price')} type={contract.get('option_type')}",
            )

    # Upstream fetch
    try:
        candles = _fetch_historical(key, unit, interval, to_d, from_d)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Historical fetch failed | key=%s | err=%s", key, exc)
        raise HTTPException(status_code=502, detail=f"upstream historical fetch failed: {exc}")

    return CandleResponse(
        found=True,
        instrument_key=key,
        index_name=index_name,
        source="upstream",
        unit=unit,
        interval=interval,
        from_date=from_d,
        to_date=to_d,
        count=len(candles),
        candles=candles,
    )


@router.get("/candles/intraday", response_model=CandleResponse)
def get_intraday(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    unit: str = Query("minutes"),
    interval: str = Query("1"),
) -> CandleResponse:
    index_name, contract = _resolve_contract(instrument_key, index, strike, option_type, expiry)
    key = contract.get("instrument_key")

    try:
        candles = _fetch_intraday(key, unit, interval)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Intraday fetch failed | key=%s | err=%s", key, exc)
        raise HTTPException(status_code=502, detail=f"upstream intraday fetch failed: {exc}")

    today_iso = date.today().isoformat()
    return CandleResponse(
        found=True,
        instrument_key=key,
        index_name=index_name,
        source="upstream",
        unit=unit,
        interval=interval,
        from_date=today_iso,
        to_date=today_iso,
        count=len(candles),
        candles=candles,
    )


@router.get("/candles", response_model=CombinedCandleResponse)
def get_candles(
    instrument_key: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD, default today-7"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD, default yesterday"),
    unit: str = Query("minutes"),
    interval: str = Query("1"),
    source: str = Query("auto", pattern="^(auto|readonly|upstream)$"),
) -> CombinedCandleResponse:
    index_name, contract = _resolve_contract(instrument_key, index, strike, option_type, expiry)
    key = contract.get("instrument_key")

    today = date.today()
    yesterday = today - timedelta(days=1)
    to_d = to_date or yesterday.isoformat()
    from_d = from_date or (today - timedelta(days=7)).isoformat()

    # historical part
    historical: CandleResponse
    if source in ("auto", "readonly"):
        stored = _read_stored(index_name, contract)
        if stored:
            sliced = _slice_by_date(stored.get("candles", []), from_d, to_d)
            historical = CandleResponse(
                found=True,
                instrument_key=key,
                index_name=index_name,
                source="readonly",
                unit=stored.get("unit", unit),
                interval=stored.get("interval", interval),
                from_date=from_d,
                to_date=to_d,
                count=len(sliced),
                candles=sliced,
            )
        else:
            historical = _historical_upstream(key, index_name, unit, interval, from_d, to_d)
    else:
        historical = _historical_upstream(key, index_name, unit, interval, from_d, to_d)

    # intraday part (always upstream — readonly never holds today)
    try:
        intraday_candles = _fetch_intraday(key, unit, interval)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Intraday fetch failed | key=%s | err=%s", key, exc)
        raise HTTPException(status_code=502, detail=f"upstream intraday fetch failed: {exc}")

    today_iso = date.today().isoformat()
    intraday = CandleResponse(
        found=True,
        instrument_key=key,
        index_name=index_name,
        source="upstream",
        unit=unit,
        interval=interval,
        from_date=today_iso,
        to_date=today_iso,
        count=len(intraday_candles),
        candles=intraday_candles,
    )

    return CombinedCandleResponse(
        found=True,
        instrument_key=key,
        index_name=index_name,
        unit=unit,
        interval=interval,
        historical=historical,
        intraday=intraday,
    )


def _historical_upstream(
    key: str, index_name: str, unit: str, interval: str, from_d: str, to_d: str
) -> CandleResponse:
    try:
        candles = _fetch_historical(key, unit, interval, to_d, from_d)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Historical fetch failed | key=%s | err=%s", key, exc)
        raise HTTPException(status_code=502, detail=f"upstream historical fetch failed: {exc}")
    return CandleResponse(
        found=True,
        instrument_key=key,
        index_name=index_name,
        source="upstream",
        unit=unit,
        interval=interval,
        from_date=from_d,
        to_date=to_d,
        count=len(candles),
        candles=candles,
    )