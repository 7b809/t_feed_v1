"""
upstox_app/option_router.py
HTTP API for option contracts (fetch, cache summary, lookup, strike window).
"""
from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from upstox_app.option.option_schemas import (
    ContractLookupResponse,
    ContractsResponse,
    LoadResponse,
    OptionCacheSummary,
    StrikeWindowRequest,
)
from upstox_app.option.option_service import (
    get_cache_summary,
    get_contract_by_key,
    get_contracts_for_index,
    get_contracts_in_strike_window,
    get_index_result,
    load_enabled_indexes,
    load_single_index,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/upstox/options", tags=["upstox-options"])


@router.get("/cache", response_model=OptionCacheSummary)
def cache_summary() -> OptionCacheSummary:
    """Snapshot of what's currently loaded (from memory)."""
    logger.info("GET /upstox/options/cache")
    return OptionCacheSummary(**get_cache_summary())


@router.post("/load", response_model=LoadResponse)
def load_all() -> LoadResponse:
    """Load option chains for every enabled index in MAIN_INDEXES."""
    logger.info("POST /upstox/options/load")
    result = load_enabled_indexes()
    ok = bool(result["loaded"]) and not result["failed"]
    return LoadResponse(
        ok=ok,
        loaded=result["loaded"],
        failed=result["failed"],
        persisted=True,
        total_contracts=result["total_contracts"],
        message=f"loaded={result['loaded']} failed={result['failed']}",
    )


@router.post("/load/{index_name}", response_model=LoadResponse)
def load_one(index_name: str) -> LoadResponse:
    """Load option contracts for a single index."""
    logger.info("POST /upstox/options/load/%s", index_name)
    result = load_single_index(index_name)
    if not result:
        raise HTTPException(status_code=404, detail=f"No contracts loaded for index={index_name}")
    return LoadResponse(
        ok=True,
        loaded=[index_name.upper()],
        failed=[],
        persisted=True,
        total_contracts=result["total_contracts"],
        message=f"index={index_name} contracts={result['total_contracts']}",
    )


@router.get("/{index_name}/contracts", response_model=ContractsResponse)
def contracts_for_index(index_name: str) -> ContractsResponse:
    """Return cached option contracts for one index."""
    logger.info("GET /upstox/options/%s/contracts", index_name)
    payload = get_index_result(index_name)
    if not payload:
        raise HTTPException(status_code=404, detail=f"No cached contracts for index={index_name}")
    return ContractsResponse(
        index_name=payload["index_name"],
        nearest_expiry=payload.get("nearest_expiry"),
        total_contracts=payload.get("total_contracts", 0),
        contracts=payload.get("contracts", []),
    )


@router.get("/contract/{instrument_key:path}", response_model=ContractLookupResponse)
def contract_lookup(instrument_key: str) -> ContractLookupResponse:
    """Look up a single contract by instrument key."""
    logger.info("GET /upstox/options/contract/%s", instrument_key)
    contract = get_contract_by_key(instrument_key)
    return ContractLookupResponse(found=contract is not None, contract=contract)


@router.post("/strike-window", response_model=ContractsResponse)
def strike_window(payload: StrikeWindowRequest) -> ContractsResponse:
    """Return contracts for an index inside an inclusive strike window."""
    logger.info(
        "POST /upstox/options/strike-window | index=%s | [%s, %s] | types=%s",
        payload.index_name, payload.lower_limit, payload.upper_limit, payload.option_types,
    )
    contracts = get_contracts_in_strike_window(
        index_name=payload.index_name,
        lower_limit=payload.lower_limit,
        upper_limit=payload.upper_limit,
        option_types=payload.option_types,
    )
    return ContractsResponse(
        index_name=payload.index_name.upper(),
        nearest_expiry=(get_index_result(payload.index_name) or {}).get("nearest_expiry"),
        total_contracts=len(contracts),
        contracts=contracts,
    )