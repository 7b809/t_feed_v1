"""
upstox_app/option_service.py

Loads option contracts for every enabled index in core_config.MAIN_INDEXES,
caches them in memory, and persists snapshots through option_storage.

Storage policy:
    Single runtime tree — every successful load overwrites
    data/runtime/options/<INDEX>.json. There is no readonly tree.
"""
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import upstox_client
from upstox_client.rest import ApiException

from core.config import core_config
from core.logger import get_logger
from token_tasks.service import token_service
from upstox_app.common.config import upstox_config
from upstox_app.option.option_storage import option_storage

logger = get_logger(__name__)

_cache_lock = threading.RLock()

# ── in-memory cache ──────────────────────────────────────────────
options_cache: Dict[str, Any] = {
    "data": [],
    "by_index": {},
    "contracts_by_key": {},
    "loaded_indexes": [],
    "loaded_at": None,
    "source": "none",   # runtime | memory | upstream | none
}


# ── small helpers ────────────────────────────────────────────────
def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_option_type(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().upper()
    if text in ("CE", "CALL"):
        return "CE"
    if text in ("PE", "PUT"):
        return "PE"
    return None


def _parse_expiry_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.split()[0], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _clean_contract(raw: Dict[str, Any], index_name: str) -> Dict[str, Any]:
    expiry_date = _parse_expiry_date(raw.get("expiry"))
    expiry_text = expiry_date.strftime("%Y-%m-%d") if expiry_date else str(raw.get("expiry") or "")
    option_type = _normalize_option_type(raw.get("instrument_type") or raw.get("option_type"))
    return {
        "instrument_key": raw.get("instrument_key"),
        "instrument_type": option_type or raw.get("instrument_type"),
        "option_type": option_type,
        "strike_price": _safe_optional_float(raw.get("strike_price")),
        "expiry": expiry_text,
        "trading_symbol": raw.get("trading_symbol"),
        "underlying_type": raw.get("underlying_type"),
        "underlying_symbol": raw.get("underlying_symbol"),
        "lot_size": _safe_int(raw.get("lot_size"), 0),
        "underlying": index_name,
    }


def _build_indexes(contracts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in contracts:
        key = str(item.get("instrument_key") or "").strip()
        if key:
            by_key[key] = item
    return by_key


def _enabled_indexes() -> Dict[str, Dict[str, Any]]:
    indexes = core_config.MAIN_INDEXES or {}
    return {
        name: cfg for name, cfg in indexes.items()
        if isinstance(cfg, dict) and cfg.get("enabled")
    }


def _strike_range_for(index_name: str) -> Tuple[Optional[float], Optional[float]]:
    cfg = (core_config.MAIN_INDEXES or {}).get(index_name, {})
    lo = _safe_optional_float(cfg.get("start_instrument_range"))
    hi = _safe_optional_float(cfg.get("end_instrument_range"))

    buffer = upstox_config.OPTIONS_STRIKE_RANGE_BUFFER or 0
    if buffer and lo is not None and hi is not None:
        lo = lo - buffer
        hi = hi + buffer

    return lo, hi


def _passes_strike_range(strike: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
    if strike is None:
        return False
    if lo is not None and strike < lo:
        return False
    if hi is not None and strike > hi:
        return False
    return True


def _pick_nearest_expiry(contracts: List[Dict[str, Any]]) -> Optional[str]:
    today = datetime.now().date()
    valid = {_parse_expiry_date(c.get("expiry")) for c in contracts}
    valid = {d for d in valid if d and d >= today}
    if not valid:
        return None
    return min(valid).strftime("%Y-%m-%d")


# ── Upstox API fetch ─────────────────────────────────────────────
def _fetch_contracts(instrument_key: str) -> Optional[Dict[str, Any]]:
    token = token_service.get_access_token()
    if not token or not token.strip():
        logger.error("No cached access token — cannot fetch option contracts")
        return None

    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = token.strip()
        api_client = upstox_client.ApiClient(configuration)
        options_api = upstox_client.OptionsApi(api_client)

        response = options_api.get_option_contracts(instrument_key)
        payload = response.to_dict() if hasattr(response, "to_dict") else response
        if not isinstance(payload, dict):
            logger.error("Unexpected option-contracts payload type | type=%s", type(payload))
            return None
        return payload

    except ApiException as exc:
        logger.error("Upstox option-contracts API error | body=%s", getattr(exc, "body", exc))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected option-contracts error: %s", exc)
        return None


# ── persistence (runtime only) ───────────────────────────────────
def _persist_index(index_name: str, payload: Dict[str, Any]) -> None:
    if not upstox_config.OPTIONS_PERSIST_ON_LOAD:
        return
    option_storage.write_runtime(index_name, payload)


def _persist_meta(loaded: List[str]) -> None:
    option_storage.write_meta({
        "loaded_indexes": loaded,
        "loaded_at": options_cache.get("loaded_at"),
        "source": options_cache.get("source"),
        "total_contracts": len(options_cache.get("data", [])),
    })


# ── public loaders ───────────────────────────────────────────────
def load_single_index(
    index_name: str,
    persist: Optional[bool] = None,
    load_all_expiries: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch + cache + persist option contracts for ONE enabled index."""
    index_name = str(index_name).upper().strip()
    cfg = (core_config.MAIN_INDEXES or {}).get(index_name)
    if not cfg or not cfg.get("enabled"):
        logger.warning("Index not enabled or unknown | index=%s", index_name)
        return None

    instrument_key = str(cfg.get("instrument_key") or "").strip()
    if not instrument_key:
        logger.error("Index has no instrument_key | index=%s", index_name)
        return None

    effective_all_expiries = (
        load_all_expiries if load_all_expiries is not None
        else upstox_config.OPTIONS_LOAD_ALL_EXPIRIES
    )

    logger.info(
        "Loading option contracts | index=%s | key=%s | all_expiries=%s",
        index_name, instrument_key, effective_all_expiries,
    )
    raw = _fetch_contracts(instrument_key)
    if not raw:
        return None

    strike_lo, strike_hi = _strike_range_for(index_name)

    cleaned: List[Dict[str, Any]] = []
    for item in raw.get("data", []) or []:
        if not isinstance(item, dict):
            continue
        contract = _clean_contract(item, index_name)

        if _normalize_option_type(contract.get("option_type") or contract.get("instrument_type")) is None:
            continue

        if not _passes_strike_range(contract.get("strike_price"), strike_lo, strike_hi):
            continue

        cleaned.append(contract)

    nearest_expiry = _pick_nearest_expiry(cleaned)
    if not effective_all_expiries and upstox_config.OPTIONS_DEFAULT_NEAREST_EXPIRY_ONLY and nearest_expiry:
        cleaned = [c for c in cleaned if c.get("expiry") == nearest_expiry]

    ce_count = sum(1 for c in cleaned if (c.get("option_type") or "").upper() == "CE")
    pe_count = sum(1 for c in cleaned if (c.get("option_type") or "").upper() == "PE")

    result = {
        "status": raw.get("status", "success"),
        "index_name": index_name,
        "instrument_key": instrument_key,
        "nearest_expiry": nearest_expiry,
        "all_expiries": effective_all_expiries,
        "strike_from": strike_lo,
        "strike_to": strike_hi,
        "total_contracts": len(cleaned),
        "ce_count": ce_count,
        "pe_count": pe_count,
        "loaded_at": _now_iso(),
        "contracts": cleaned,
    }

    with _cache_lock:
        options_cache["by_index"][index_name] = result
        options_cache["data"] = [
            c for idx in options_cache["by_index"].values() for c in idx.get("contracts", [])
        ]
        options_cache["contracts_by_key"] = _build_indexes(options_cache["data"])
        options_cache["loaded_indexes"] = sorted(options_cache["by_index"].keys())
        options_cache["loaded_at"] = _now_iso()
        options_cache["source"] = "upstream"

    logger.info(
        "Option contracts loaded | index=%s | all_expiries=%s | nearest_expiry=%s | "
        "total=%d | CE=%d | PE=%d | range=[%s, %s]",
        index_name, effective_all_expiries, nearest_expiry,
        len(cleaned), ce_count, pe_count, strike_lo, strike_hi,
    )

    if persist if persist is not None else upstox_config.OPTIONS_PERSIST_ON_LOAD:
        _persist_index(index_name, result)

    return result


def load_enabled_indexes(
    persist: Optional[bool] = None,
    load_all_expiries: Optional[bool] = None,
) -> Dict[str, Any]:
    """Load option contracts for every enabled index in MAIN_INDEXES."""
    enabled = list(_enabled_indexes().keys())
    effective_all_expiries = (
        load_all_expiries if load_all_expiries is not None
        else upstox_config.OPTIONS_LOAD_ALL_EXPIRIES
    )

    logger.info(
        "Loading options for enabled indexes | indexes=%s | all_expiries=%s",
        enabled, effective_all_expiries,
    )

    loaded: List[str] = []
    failed: List[str] = []

    for name in enabled:
        try:
            result = load_single_index(
                name, persist=persist, load_all_expiries=effective_all_expiries,
            )
            if result:
                loaded.append(name)
            else:
                failed.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Option load failed | index=%s | err=%s", name, exc)
            failed.append(name)

    if upstox_config.OPTIONS_PERSIST_ON_LOAD:
        _persist_meta(loaded)

    return {
        "loaded": loaded,
        "failed": failed,
        "total_contracts": len(options_cache.get("data", [])),
        "all_expiries": effective_all_expiries,
    }


# ── startup hydration from disk ──────────────────────────────────
def hydrate_from_runtime() -> int:
    """Populate the in-memory cache from fresh runtime snapshots on disk."""
    if not upstox_config.OPTIONS_LOAD_RUNTIME_ON_STARTUP:
        logger.info("Runtime hydration disabled")
        return 0

    hydrated: List[str] = []
    with _cache_lock:
        for name in option_storage.list_runtime_indexes():
            if not option_storage.is_runtime_fresh(name):
                logger.info("Skipping stale runtime snapshot | index=%s", name)
                continue
            payload = option_storage.read_runtime(name)
            if not payload:
                continue
            options_cache["by_index"][name] = payload
            hydrated.append(name)

        if hydrated:
            options_cache["data"] = [
                c for idx in options_cache["by_index"].values() for c in idx.get("contracts", [])
            ]
            options_cache["contracts_by_key"] = _build_indexes(options_cache["data"])
            options_cache["loaded_indexes"] = sorted(options_cache["by_index"].keys())
            options_cache["loaded_at"] = _now_iso()
            options_cache["source"] = "runtime"

    if hydrated:
        logger.info(
            "Hydrated option cache from runtime | indexes=%s | contracts=%d",
            hydrated, len(options_cache.get("data", [])),
        )
    else:
        logger.info("No fresh runtime snapshots to hydrate")
    return len(hydrated)


# ── cache reads ──────────────────────────────────────────────────
def get_index_result(index_name: str) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        return options_cache["by_index"].get(str(index_name).upper().strip())


def get_all_contracts() -> List[Dict[str, Any]]:
    with _cache_lock:
        return list(options_cache.get("data", []))


def get_contracts_for_index(index_name: str) -> List[Dict[str, Any]]:
    with _cache_lock:
        payload = options_cache["by_index"].get(str(index_name).upper().strip())
        return list(payload.get("contracts", [])) if payload else []


def get_contract_by_key(instrument_key: str) -> Optional[Dict[str, Any]]:
    key = str(instrument_key or "").strip()
    if not key:
        return None
    with _cache_lock:
        return options_cache.get("contracts_by_key", {}).get(key)


def get_contracts_in_strike_window(
    index_name: str,
    lower_limit: float,
    upper_limit: float,
    option_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    lo = _safe_float(lower_limit)
    hi = _safe_float(upper_limit)
    wanted = None
    if option_types:
        wanted = {_normalize_option_type(t) for t in option_types}
        wanted.discard(None)

    output = []
    for contract in get_contracts_for_index(index_name):
        strike = _safe_optional_float(contract.get("strike_price"))
        opt_type = _normalize_option_type(contract.get("option_type") or contract.get("instrument_type"))
        if strike is None or not opt_type:
            continue
        if wanted and opt_type not in wanted:
            continue
        if lo <= strike <= hi:
            output.append(contract)
    return output


# ── bulk-subscribe helper ────────────────────────────────────────
def get_subscribable_option_keys() -> Tuple[List[str], Dict[str, int]]:
    """
    Return every cached CE/PE instrument_key from enabled indexes plus
    a per-index breakdown. Deduplicated, order-preserving.
    """
    enabled = set(_enabled_indexes().keys())
    keys: List[str] = []
    per_index: Dict[str, int] = {}

    with _cache_lock:
        for index_name, payload in options_cache.get("by_index", {}).items():
            if index_name not in enabled:
                continue
            count = 0
            for contract in payload.get("contracts", []):
                key = str(contract.get("instrument_key") or "").strip()
                if not key:
                    continue
                keys.append(key)
                count += 1
            per_index[index_name] = count

    seen = set()
    deduped: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)

    logger.info(
        "Subscribable option keys resolved | total=%d | per_index=%s",
        len(deduped), per_index,
    )
    return deduped, per_index


def get_cache_summary() -> Dict[str, Any]:
    with _cache_lock:
        enabled = list(_enabled_indexes().keys())
        per_index = []
        for name, cfg in (core_config.MAIN_INDEXES or {}).items():
            payload = options_cache["by_index"].get(name)
            lo, hi = _strike_range_for(name)
            per_index.append({
                "index_name": name,
                "enabled": bool(cfg.get("enabled")),
                "instrument_key": cfg.get("instrument_key"),
                "nearest_expiry": payload.get("nearest_expiry") if payload else None,
                "all_expiries": payload.get("all_expiries") if payload else None,
                "total_contracts": len(payload.get("contracts", [])) if payload else 0,
                "ce_count": payload.get("ce_count") if payload else 0,
                "pe_count": payload.get("pe_count") if payload else 0,
                "strike_from": lo,
                "strike_to": hi,
                "loaded_at": payload.get("loaded_at") if payload else None,
                "source": "memory" if payload else "none",
            })

        return {
            "total_contracts": len(options_cache.get("data", [])),
            "enabled_indexes": enabled,
            "loaded_indexes": list(options_cache.get("loaded_indexes", [])),
            "per_index": per_index,
            "runtime_dir": str(option_storage.runtime_root.resolve()),
        }