"""
Data-access helpers used by the web templates and APIs.

Everything here is intentionally defensive: if an upstream service or a
disk snapshot is missing, we return safe defaults instead of raising, so
the UI can still render.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
READONLY_DIR = DATA_DIR / "readonly"
RUNTIME_DIR = DATA_DIR / "runtime"
OPTIONS_RUNTIME_DIR = RUNTIME_DIR / "options"
LOGS_DIR = Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs"))
RUNTIME_REFRESH_STATUS = RUNTIME_DIR / "refresh_status.json"

APP_NAME = os.getenv("APP_NAME", "UpstoxAppV2")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(IST)


def format_ist(dt: Optional[datetime] = None) -> str:
    dt = dt or now_ist()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Option contracts
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Optional[Any]:
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to read JSON %s: %s", path, exc)
        return None


def list_enabled_indexes() -> List[str]:
    """Best-effort list of enabled index names."""
    try:
        from upstox_app.common.config import MAIN_INDEXES  # type: ignore

        if isinstance(MAIN_INDEXES, dict):
            return [str(k).upper() for k in MAIN_INDEXES.keys()]
        if isinstance(MAIN_INDEXES, (list, tuple)):
            return [str(x).upper() for x in MAIN_INDEXES]
    except Exception:
        pass
    return ["NIFTY", "SENSEX"]


# ---------------------------------------------------------------------------
# Index -> Upstox index instrument-key mapping.
# ---------------------------------------------------------------------------
INDEX_INSTRUMENT_KEYS: Dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "SENSEX": "BSE_INDEX|SENSEX",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
}


def index_meta() -> List[Dict[str, str]]:
    """
    Return a list of {name, display_name, instrument_key} for every enabled
    index, ready to render as cards on /charts.
    """
    metas: List[Dict[str, str]] = []
    for name in list_enabled_indexes():
        upper = name.upper()
        metas.append(
            {
                "name": upper,
                "display_name": name,
                "instrument_key": INDEX_INSTRUMENT_KEYS.get(
                    upper, f"NSE_INDEX|{name}"
                ),
            }
        )
    return metas


def load_option_contracts(index_name: str) -> List[Dict[str, Any]]:
    """
    Load cached option contracts for `index_name`.

    Tries the option service first, then falls back to the runtime snapshot.
    """
    index_name = index_name.upper()

    # 1) Try live option service
    try:
        from upstox_app.option.option_service import option_service  # type: ignore

        for attr in ("get_contracts", "get_cached_contracts", "get_index_contracts"):
            fn = getattr(option_service, attr, None)
            if callable(fn):
                try:
                    result = fn(index_name)
                    if result:
                        return _normalise_contracts(result)
                except Exception:
                    continue
    except Exception:
        pass

    # 2) Fallback to runtime snapshot
    snapshot = _read_json(OPTIONS_RUNTIME_DIR / f"{index_name}.json")
    if snapshot:
        return _normalise_contracts(snapshot)

    return []


def _normalise_contracts(raw: Any) -> List[Dict[str, Any]]:
    """Coerce whatever the service/snapshot returns into a flat list of dicts."""
    items: List[Dict[str, Any]] = []

    if isinstance(raw, dict):
        for key in ("contracts", "instruments", "data", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            # dict keyed by instrument_key
            for k, v in raw.items():
                if isinstance(v, dict):
                    item = dict(v)
                    item.setdefault("instrument_key", k)
                    items.append(item)
            return _dedupe([_fill_common_fields(x) for x in items])

    if not isinstance(raw, list):
        return []

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
        merged = {**info, **entry}
        items.append(merged)

    return _dedupe([_fill_common_fields(x) for x in items])


def _fill_common_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort fill of the fields the templates depend on:
    option_type, strike_price, expiry, trading_symbol, etc.
    """
    # option_type
    if not item.get("option_type"):
        for k in ("instrument_type", "optionType", "option", "type"):
            if item.get(k):
                item["option_type"] = str(item[k]).upper()
                break

    # strike_price
    if item.get("strike_price") in (None, ""):
        for k in ("strike", "strikePrice", "strike_price"):
            if item.get(k) not in (None, ""):
                item["strike_price"] = item[k]
                break

    # expiry
    if not item.get("expiry"):
        for k in ("expiry_date", "expiryDate", "expiry"):
            if item.get(k):
                item["expiry"] = item[k]
                break

    # trading_symbol
    if not item.get("trading_symbol"):
        for k in ("tradingsymbol", "symbol", "name"):
            if item.get(k):
                item["trading_symbol"] = item[k]
                break

    return item


def _dedupe(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        key = item.get("instrument_key") or item.get("instrument_token")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def load_all_option_instruments() -> List[Dict[str, Any]]:
    """Combine contracts from every enabled index (used by /charts)."""
    combined: List[Dict[str, Any]] = []
    seen = set()
    for idx in list_enabled_indexes():
        for c in load_option_contracts(idx):
            key = c.get("instrument_key") or c.get("instrument_token")
            if not key or key in seen:
                continue
            seen.add(key)
            c.setdefault("index", idx)
            combined.append(c)
    return combined


def find_contract_by_key(instrument_key: str) -> Optional[Dict[str, Any]]:
    for c in load_all_option_instruments():
        if c.get("instrument_key") == instrument_key:
            return c
    return None


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------
def _candle_path_for(index_name: str, strike: Any, option_type: str) -> Optional[Path]:
    try:
        strike_int = int(float(strike))
    except Exception:
        return None
    opt = str(option_type).upper()
    if opt not in ("CE", "PE"):
        return None
    return READONLY_DIR / index_name.upper() / "candles" / f"{strike_int}_{opt}.json"


def load_candles_for_contract(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Load the merged candle list for a contract from the readonly cache."""
    index_name = (
        contract.get("index")
        or contract.get("underlying_symbol")
        or contract.get("underlying")
    )
    strike = contract.get("strike_price")
    opt_type = contract.get("option_type") or contract.get("instrument_type")

    if not (index_name and strike and opt_type):
        return []

    path = _candle_path_for(str(index_name), strike, str(opt_type))
    if not path or not path.exists():
        return []

    payload = _read_json(path) or {}
    candles = payload.get("candles") if isinstance(payload, dict) else payload
    if not isinstance(candles, list):
        return []

    normalised: List[Dict[str, Any]] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        try:
            normalised.append(
                {
                    "time": int(c.get("time") or c.get("timestamp") or c.get("ts")),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                }
            )
        except Exception:
            continue

    normalised.sort(key=lambda x: x["time"])
    return normalised


def load_candles_for_key(instrument_key: str) -> List[Dict[str, Any]]:
    """Candles for an instrument key, routed through the current cache."""
    contract = find_contract_by_key(instrument_key) or {}
    return load_candles_for_contract(contract)


def load_historical_before(
    instrument_key: str, before_date: str
) -> Dict[str, Any]:
    """
    Return candles older than `before_date` (YYYY-MM-DD) for the given key.

    Falls back to an empty result when nothing is cached.
    """
    all_candles = load_candles_for_key(instrument_key)

    try:
        cutoff = datetime.strptime(before_date, "%Y-%m-%d").replace(tzinfo=IST)
        cutoff_ts = int(cutoff.timestamp())
    except Exception:
        return {"candles": [], "has_more": False}

    older = [c for c in all_candles if c["time"] < cutoff_ts]
    return {"candles": older, "has_more": False}


# ---------------------------------------------------------------------------
# Refresh status
# ---------------------------------------------------------------------------
DEFAULT_REFRESH_STATUS = {
    "manual_refresh_running": False,
    "last_manual_refresh": {
        "status": "idle",
        "timestamp": None,
        "nearest_expiry": None,
        "subscribed_instruments": 0,
        "message": "No manual refresh triggered yet.",
    },
}


def read_refresh_status() -> Dict[str, Any]:
    data = _read_json(RUNTIME_REFRESH_STATUS)
    if not isinstance(data, dict):
        return dict(DEFAULT_REFRESH_STATUS)
    merged = dict(DEFAULT_REFRESH_STATUS)
    merged.update(data)
    return merged


def write_refresh_status(payload: Dict[str, Any]) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with RUNTIME_REFRESH_STATUS.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to persist refresh status: %s", exc)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
def list_log_files() -> List[Dict[str, Any]]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    entries: List[Dict[str, Any]] = []
    for path in sorted(LOGS_DIR.glob("*.log")):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    return entries


def read_log_file(filename: str, max_bytes: int = 2_000_000) -> str:
    # Security: prevent path traversal.
    safe_name = Path(filename).name
    path = LOGS_DIR / safe_name
    if not path.exists() or not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        raw = fh.read()
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Orders — delegates to upstox_app.order_book_service
# ---------------------------------------------------------------------------
def load_orders() -> Dict[str, Any]:
    """
    Fetch today's orders through `OrderBookService`.

    Returns a payload shaped for the orders page:
        {
            "success": bool,
            "data": [ ...normalised orders... ],
            "message": str,      # present when success is True or on failure
            "error": str,        # optional, on failure
        }

    Never raises.
    """
    try:
        from upstox_app.portfolio.order_book_service import order_book_service  # type: ignore
    except Exception as exc:
        logger.warning("OrderBookService unavailable: %s", exc)
        return {
            "success": False,
            "data": [],
            "message": f"Order service unavailable: {exc}",
            "error": str(exc),
        }

    try:
        result = order_book_service.get_all_orders()
    except Exception as exc:
        logger.exception("OrderBookService.get_all_orders() raised")
        return {
            "success": False,
            "data": [],
            "message": str(exc),
            "error": str(exc),
        }

    if not isinstance(result, dict):
        return {
            "success": False,
            "data": [],
            "message": "Order service returned an invalid payload.",
        }

    if not result.get("success"):
        return {
            "success": False,
            "data": [],
            "message": result.get("message") or "Failed to fetch orders.",
            "error": result.get("message") or "Failed to fetch orders.",
        }

    return {
        "success": True,
        "data": result.get("data") or [],
        "message": result.get("status") or "OK",
    }