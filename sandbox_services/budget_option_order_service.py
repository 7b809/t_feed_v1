"""
Budget-filtered option contract order orchestration.

The budget is an inclusive per-contract premium range based on the option-chain
market_data.ltp field. It is not the total order value or required margin.

Execution is disabled by default. With execute=True the service selects the
lowest-LTP matches (up to max_orders) and delegates each order to the existing
sandbox_services.place_order_service.
"""
from __future__ import annotations

import math
from typing import Any, Optional
from urllib.parse import unquote

from core.logger import get_logger
from sandbox_services.option_contract_service import (
    UNDERLYING_SYMBOL_MAP,
    get_nearest_expiry_option_contracts,
)
from sandbox_services.place_order_service import place_order as place_order_service
from services.daily_profit_guard_service import DailyProfitGuardError, assert_new_entries_allowed

logger = get_logger(__file__)

DEFAULT_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
_SYMBOL_TO_KEY = {symbol.upper(): key for key, symbol in UNDERLYING_SYMBOL_MAP.items()}


def normalize_underlying_key(value: Optional[str]) -> str:
    """Accept mapped aliases such as NIFTY/SENSEX or a URL-encoded instrument key."""
    raw = (value or DEFAULT_UNDERLYING_KEY).strip() or DEFAULT_UNDERLYING_KEY
    symbol_key = _SYMBOL_TO_KEY.get(raw.upper())
    if symbol_key:
        return symbol_key
    clean = unquote(raw).strip()
    return _SYMBOL_TO_KEY.get(clean.upper(), clean)


def _failed(step: str, code: str, message: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "step": step,
        "error": {"error_code": code, "message": message},
        "context": context,
    }


def _finite_positive(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number.") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} must be a finite number greater than zero.")
    return number


def place_option_orders_by_budget(
    *,
    underlying_key: Optional[str] = None,
    budget_min: float,
    budget_max: float,
    option_type: str,
    expiry: Optional[str] = None,
    offset_override: Optional[float] = None,
    execute: bool = False,
    max_orders: int = 1,
    lot: int = 1,
    transaction_type: str = "BUY",
    product: str = "D",
    order_type: str = "MARKET",
    validity: str = "DAY",
    is_amo: bool = False,
    tag: Optional[str] = None,
    slice_order: bool = True,
    strict_lot_size: bool = True,
) -> dict[str, Any]:
    """
    Preview or execute selected CE/PE option contracts.

    Matching is inclusive: budget_min <= option-chain LTP <= budget_max.
    Matches are sorted by lowest premium first. If several contracts match,
    the first max_orders cheapest contracts are selected; ATM distance and
    strike are deterministic tie-breakers. The complete matching list is
    always returned for review.
    """
    try:
        clean_key = normalize_underlying_key(underlying_key)
        side = str(option_type or "").strip().upper()
        tx_type = str(transaction_type or "BUY").strip().upper()
        product_code = str(product or "D").strip().upper()
        order_kind = str(order_type or "LIMIT").strip().upper()
        validity_code = str(validity or "DAY").strip().upper()

        minimum = _finite_positive(budget_min, "budget_min")
        maximum = _finite_positive(budget_max, "budget_max")
        if maximum < minimum:
            raise ValueError("budget_max must be greater than or equal to budget_min.")
        if side not in {"CE", "PE"}:
            raise ValueError("option_type must be CE or PE.")
        if tx_type not in {"BUY", "SELL"}:
            raise ValueError("transaction_type must be BUY or SELL.")
        if product_code not in {"D", "I"}:
            raise ValueError("product must be D or I.")
        if order_kind not in {"MARKET", "LIMIT"}:
            raise ValueError("order_type must be MARKET or LIMIT.")
        if validity_code not in {"DAY", "IOC"}:
            raise ValueError("validity must be DAY or IOC.")
        if isinstance(max_orders, bool) or not isinstance(max_orders, int) or not 1 <= max_orders <= 10:
            raise ValueError("max_orders must be an integer between 1 and 10.")
        if isinstance(lot, bool) or not isinstance(lot, int) or lot < 1:
            raise ValueError("lot must be a positive integer.")
        if offset_override is not None:
            offset = float(offset_override)
            if not math.isfinite(offset) or offset < 0:
                raise ValueError("offset_override must be a finite non-negative number.")
        symbol = UNDERLYING_SYMBOL_MAP.get(clean_key)
        if not symbol:
            raise ValueError(
                f"Unsupported underlying '{clean_key}'. "
                f"Supported symbols: {sorted(_SYMBOL_TO_KEY)}."
            )
    except (TypeError, ValueError) as exc:
        return _failed(
            "VALIDATION",
            "VALIDATION_ERROR",
            str(exc),
            {
                "underlying_key": underlying_key,
                "budget_min": budget_min,
                "budget_max": budget_max,
                "option_type": option_type,
            },
        )

    logger.info(
        "Budget option flow started | underlying=%s | side=%s | premium=[%.2f, %.2f] | "
        "execute=%s | max_orders=%s",
        clean_key, side, minimum, maximum, execute, max_orders,
    )

    try:
        chain = get_nearest_expiry_option_contracts(
            underlying_key=clean_key,
            expiry=expiry,
            offset_override=offset_override,
            include_market_data=True,
            include_greeks=False,
            include_metadata=True,
        )
    except Exception as exc:
        logger.exception("Option chain fetch raised unexpectedly for %s", clean_key)
        return _failed(
            "OPTION_CHAIN",
            "OPTION_CHAIN_EXCEPTION",
            str(exc),
            {"underlying_key": clean_key, "expiry": expiry},
        )

    if not isinstance(chain, dict) or chain.get("status") != "SUCCESS":
        chain_error = chain.get("error") if isinstance(chain, dict) else None
        if not isinstance(chain_error, dict):
            chain_error = {}
        return _failed(
            "OPTION_CHAIN",
            chain_error.get("error_code", "OPTION_CHAIN_FAILED"),
            chain_error.get("message", "Could not fetch the option chain."),
            {
                "underlying_key": clean_key,
                "expiry": expiry,
                "chain_step": chain.get("step") if isinstance(chain, dict) else None,
                "chain_context": chain.get("context") if isinstance(chain, dict) else None,
            },
        )

    raw_contracts = chain.get("ce_contracts" if side == "CE" else "pe_contracts") or []
    matches: list[dict[str, Any]] = []
    missing_ltp_count = 0
    invalid_rows = 0
    seen_keys: set[str] = set()

    for contract in raw_contracts:
        if not isinstance(contract, dict):
            invalid_rows += 1
            continue
        instrument_key = str(contract.get("instrument_key") or "").strip()
        if not instrument_key:
            invalid_rows += 1
            continue
        if instrument_key in seen_keys:
            continue
        seen_keys.add(instrument_key)

        market_data = contract.get("market_data")
        ltp_value = market_data.get("ltp") if isinstance(market_data, dict) else None
        try:
            ltp = float(ltp_value)
        except (TypeError, ValueError):
            missing_ltp_count += 1
            continue
        if not math.isfinite(ltp) or ltp <= 0:
            missing_ltp_count += 1
            continue

        if minimum <= ltp <= maximum:
            try:
                atm_distance = abs(float(contract.get("strike_offset") or 0))
            except (TypeError, ValueError):
                atm_distance = float("inf")
            try:
                strike_price = float(contract.get("strike_price") or 0)
            except (TypeError, ValueError):
                strike_price = 0.0

            matches.append({
                "instrument_key": instrument_key,
                "instrument_type": contract.get("instrument_type") or side,
                "trading_symbol": contract.get("trading_symbol"),
                "expiry": contract.get("expiry"),
                "strike_price": contract.get("strike_price"),
                "moneyness": contract.get("moneyness"),
                "strike_offset": contract.get("strike_offset"),
                "ltp": round(ltp, 2),
                "volume": market_data.get("volume"),
                "oi": market_data.get("oi"),
                "lot_size": contract.get("lot_size"),
                "exchange": contract.get("exchange"),
                "_midpoint_distance": abs(ltp - ((minimum + maximum) / 2.0)),
                "_atm_distance": atm_distance,
                "_strike_sort": strike_price,
            })

    # The required selection rule is the lowest eligible option premium.
    # ATM distance and strike are deterministic tie-breakers only.
    matches.sort(
        key=lambda item: (
            item["ltp"],
            item["_atm_distance"],
            item["_strike_sort"],
        )
    )
    selected = matches[:max_orders]

    def public_contract(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if not key.startswith("_")}

    public_matches = [public_contract(item) for item in matches]
    public_selected = [public_contract(item) for item in selected]

    response: dict[str, Any] = {
        "status": "PREVIEW" if selected and not execute else "NO_MATCHES" if not selected else "EXECUTING",
        "mode": "EXECUTE" if execute else "DRY_RUN",
        "underlying_key": clean_key,
        "underlying_symbol": chain.get("underlying_symbol") or symbol,
        "underlying_ltp": chain.get("underlying_ltp"),
        "expiry_requested": expiry,
        "expiry_keyword": chain.get("expiry_keyword"),
        "expiry_used": chain.get("expiry_used"),
        "option_type": side,
        "execution_settings": {
            "transaction_type": tx_type,
            "product": product_code,
            "order_type": order_kind,
            "lot": lot,
            "validity": validity_code,
            "is_amo": is_amo,
            "slice_order": slice_order,
        },
        "budget_range": {
            "min": minimum,
            "max": maximum,
            "basis": "option_contract_market_data_ltp",
        },
        "matching_contracts_count": len(matches),
        "selected_contracts_count": len(selected),
        "max_orders": max_orders,
        "contracts_missing_ltp_count": missing_ltp_count,
        "invalid_contract_rows": invalid_rows,
        "matching_contracts": public_matches,
        "selected_contracts": public_selected,
        "selected_instrument": public_selected[0] if public_selected else None,
        "selected_instrument_key": (
            public_selected[0].get("instrument_key") if public_selected else None
        ),
        "placement_results": [],
        "note": (
            "Budget range filters each option contract's observed premium (LTP), "
            "not total lot value or required margin. Execution is opt-in."
        ),
    }

    if not execute or not selected:
        return response

    placement_results: list[dict[str, Any]] = []
    successful = 0
    failed = 0
    guard_failure: dict[str, Any] | None = None

    for index, contract in enumerate(selected, start=1):
        instrument_key = contract["instrument_key"]

        # The established daily-profit guard applies to new BUY entries.
        # Re-check before each broker submission so a multi-contract batch
        # cannot continue opening new BUY orders after the daily lock trips.
        if tx_type == "BUY":
            try:
                assert_new_entries_allowed()
            except DailyProfitGuardError as exc:
                guard_failure = {
                    "error_code": exc.code,
                    "message": str(exc),
                    "http_status": exc.status_code,
                    "details": exc.details,
                }
            except Exception as exc:
                logger.exception("Unexpected daily-profit guard failure before option order")
                guard_failure = {
                    "error_code": "DAILY_PROFIT_CHECK_FAILED",
                    "message": "Could not verify the daily profit guard; remaining BUY orders were blocked.",
                    "http_status": 503,
                }

            if guard_failure:
                for blocked_index, blocked_contract in enumerate(selected[index - 1:], start=index):
                    failed += 1
                    placement_results.append({
                        "sequence": blocked_index,
                        "instrument_key": blocked_contract.get("instrument_key"),
                        "trading_symbol": blocked_contract.get("trading_symbol"),
                        "option_type": side,
                        "strike_price": blocked_contract.get("strike_price"),
                        "selected_ltp": blocked_contract.get("ltp"),
                        "status": "BLOCKED",
                        "step": "DAILY_PROFIT_GUARD",
                        "error": guard_failure,
                    })
                break
        lot_size_override = None
        try:
            metadata_lot_size = int(contract.get("lot_size"))
            if metadata_lot_size > 0:
                lot_size_override = metadata_lot_size
        except (TypeError, ValueError):
            pass

        try:
            place_result = place_order_service(
                instrument_token=instrument_key,
                order_type=order_kind,
                transaction_type=tx_type,
                product=product_code,
                lot=lot,
                entry=contract["ltp"] if order_kind == "LIMIT" else None,
                symbol=symbol,
                lot_size_override=lot_size_override,
                validity=validity_code,
                is_amo=is_amo,
                tag=tag,
                slice_order=slice_order,
                strict_lot_size=strict_lot_size,
            )
            if not isinstance(place_result, dict):
                place_result = {
                    "status": "FAILED",
                    "step": "ORDER_PLACEMENT",
                    "error": {
                        "error_code": "INVALID_ORDER_SERVICE_RESPONSE",
                        "message": "Existing place_order_service returned an invalid response.",
                    },
                }
        except Exception as exc:
            logger.exception(
                "Unexpected budget-option placement exception | key=%s",
                instrument_key,
            )
            place_result = {
                "status": "FAILED",
                "step": "ORDER_PLACEMENT",
                "error": {
                    "error_code": "UNEXPECTED_ORDER_SERVICE_EXCEPTION",
                    "message": str(exc),
                },
            }

        was_successful = str(place_result.get("status") or "").upper() == "SUCCESS"
        if was_successful:
            successful += 1
        else:
            failed += 1

        placement_results.append({
            "sequence": index,
            "instrument_key": instrument_key,
            "trading_symbol": contract.get("trading_symbol"),
            "option_type": side,
            "strike_price": contract.get("strike_price"),
            "selected_ltp": contract["ltp"],
            "status": place_result.get("status", "UNKNOWN"),
            "step": place_result.get("step"),
            "order_response": place_result,
            "error": place_result.get("error"),
        })
        logger.info(
            "Budget option placement %s/%s | key=%s | status=%s",
            index, len(selected), instrument_key, place_result.get("status"),
        )

    response["placement_results"] = placement_results
    response["successful_orders"] = successful
    response["failed_orders"] = failed
    if guard_failure:
        response["daily_profit_guard"] = guard_failure

    if successful and failed:
        response["status"] = "PARTIAL_SUCCESS"
    elif successful:
        response["status"] = "SUCCESS"
    else:
        response["status"] = "FAILED"
        if guard_failure:
            response["step"] = "DAILY_PROFIT_GUARD"
            response["error"] = {
                "error_code": guard_failure["error_code"],
                "message": guard_failure["message"],
                "http_status": guard_failure.get("http_status"),
            }
        else:
            response["step"] = "ORDER_PLACEMENT"
            response["error"] = {
                "error_code": "ALL_ORDERS_FAILED",
                "message": "None of the selected option orders was successfully placed.",
            }
        response["context"] = {
            "placement_results": placement_results,
            "underlying_key": clean_key,
            "option_type": side,
            "daily_profit_guard": guard_failure,
        }
    return response
