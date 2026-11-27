"""
Budget-filtered option contract order orchestration.

The budget is an inclusive per-contract premium range based on the option-chain
market_data.ltp field. It is not the total order value or required margin.

Execution is disabled by default. With execute=True the service selects the
lowest-LTP matches (up to max_orders) and delegates each order to the existing
sandbox_services.place_order_service.

Every returned contract carries:
  * trading_symbol / lot_size / exchange    (enriched, never null on success)
  * premium_per_unit / premium_per_lot / premium_total / required_margin
Failure responses surface the dominant Upstox UDAPI error code so callers can
map it to the correct HTTP status.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional
from urllib.parse import unquote

from core.logger import get_logger
from core.upstox_errors import lookup as lookup_upstox_error
from sandbox_services.option_contract_service import (
    UNDERLYING_SYMBOL_MAP,
    get_nearest_expiry_option_contracts,
)
from sandbox_services.place_order_service import place_order as place_order_service
from services.daily_profit_guard_service import DailyProfitGuardError, assert_new_entries_allowed

logger = get_logger(__file__)

DEFAULT_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
_SYMBOL_TO_KEY = {symbol.upper(): key for key, symbol in UNDERLYING_SYMBOL_MAP.items()}

_SYMBOL_KEYS = ("trading_symbol", "tradingSymbol", "tradingsymbol", "symbol")
_LOT_SIZE_KEYS = ("lot_size", "lotSize", "lot")
_EXCHANGE_KEYS = ("exchange", "exchange_segment", "segment")

# Fallback lot sizes — used only when the option chain omits lot_size AND the
# metadata endpoint is unavailable. Current NSE/BSE contract sizes.
_SYMBOL_LOT_SIZE_FALLBACK: dict[str, int] = {
    "NIFTY": 65,            # was 75; Upstox currently returns 65
    "BANKNIFTY": 35,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "SENSEX": 20,
    "BANKEX": 30,
}

# Rough SPAN + Exposure as a fraction of notional for short options.
_SELL_MARGIN_NOTIONAL_FRACTION = 0.12


def normalize_underlying_key(value: Optional[str]) -> str:
    raw = (value or DEFAULT_UNDERLYING_KEY).strip() or DEFAULT_UNDERLYING_KEY
    symbol_key = _SYMBOL_TO_KEY.get(raw.upper())
    if symbol_key:
        return symbol_key
    clean = unquote(raw).strip()
    return _SYMBOL_TO_KEY.get(clean.upper(), clean)


def _failed(step: str, code: str, message: str, context: dict[str, Any]) -> dict[str, Any]:
    meta = lookup_upstox_error(code)
    return {
        "status": "FAILED",
        "step": step,
        "error": {
            "error_code": code,
            "category": meta.category if meta else "service",
            "http_status": meta.http_status if meta else None,
            "title": meta.title if meta else None,
            "message": message,
            "remediation": meta.remediation if meta else None,
        },
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


# ---------------------------------------------------------------------------
# Contract field extraction
# ---------------------------------------------------------------------------
def _nested_meta(contract: dict[str, Any]) -> dict[str, Any]:
    for container_key in ("metadata", "instrument", "instrument_meta"):
        nested = contract.get(container_key)
        if isinstance(nested, dict):
            return nested
    return {}


def _pick_contract_field(contract: dict[str, Any], *keys: str) -> Any:
    meta = _nested_meta(contract)
    for source in (contract, meta):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _derive_trading_symbol(
    contract: dict[str, Any],
    underlying_symbol: Optional[str],
    side: str,
) -> Optional[str]:
    strike_raw = contract.get("strike_price")
    try:
        strike = int(float(strike_raw))
    except (TypeError, ValueError):
        return None
    expiry_label = ""
    expiry_raw = contract.get("expiry")
    if isinstance(expiry_raw, str) and expiry_raw:
        try:
            iso = expiry_raw.replace("Z", "+00:00").split("+", 1)[0].split("T", 1)[0]
            expiry_label = datetime.fromisoformat(iso).strftime("%d %b %y").upper()
        except ValueError:
            expiry_label = ""
    parts = [underlying_symbol or "", str(strike), side]
    if expiry_label:
        parts.append(expiry_label)
    return " ".join(part for part in parts if part)


def _resolve_lot_size(
    contract: dict[str, Any],
    symbol: Optional[str],
) -> tuple[Optional[int], str]:
    meta_lot = _pick_contract_field(contract, *_LOT_SIZE_KEYS)
    try:
        if meta_lot is not None:
            value = int(meta_lot)
            if value > 0:
                return value, "metadata"
    except (TypeError, ValueError):
        pass
    if symbol:
        fallback = _SYMBOL_LOT_SIZE_FALLBACK.get(symbol.upper())
        if fallback:
            return fallback, "symbol_fallback"
    return None, "unresolved"


def _resolve_exchange(instrument_key: str) -> tuple[Optional[str], str]:
    if "|" in instrument_key:
        segment = instrument_key.split("|", 1)[0].strip()
        if segment:
            return segment, "instrument_key"
    return None, "unresolved"


def _compute_lot_margin(
    *,
    ltp: float,
    lot_size: Optional[int],
    lots: int,
    transaction_type: str,
    underlying_ltp: Optional[float],
    strike_price: Optional[float],
) -> dict[str, Any]:
    if not lot_size or lot_size <= 0:
        return {
            "units_per_lot": None,
            "premium_per_unit": round(ltp, 2),
            "premium_per_lot": None,
            "premium_total": None,
            "required_margin": None,
            "margin_basis": "unresolved_missing_lot_size",
        }

    units = lot_size * lots
    premium_per_lot = ltp * lot_size
    premium_total = ltp * units

    if transaction_type == "BUY":
        required_margin = premium_total
        basis = "premium_only_buy"
    else:
        notional_base = underlying_ltp if underlying_ltp else strike_price
        try:
            notional = float(notional_base) * units
        except (TypeError, ValueError):
            notional = 0.0
        estimate = notional * _SELL_MARGIN_NOTIONAL_FRACTION
        required_margin = max(premium_total, estimate)
        basis = "estimated_span_plus_exposure_12pct"

    return {
        "units_per_lot": lot_size,
        "premium_per_unit": round(ltp, 2),
        "premium_per_lot": round(premium_per_lot, 2),
        "premium_total": round(premium_total, 2),
        "required_margin": round(required_margin, 2),
        "margin_basis": basis,
    }


# ---------------------------------------------------------------------------
# Aggregate broker failure
# ---------------------------------------------------------------------------
def _aggregate_failure(placement_results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Determine the dominant error across all failed placements.

    Preference order:
      1. The first broker (UDAPI*) error_code seen.
      2. The first non-UDAPI error_code.
      3. ``ALL_ORDERS_FAILED`` as a last resort.
    The dominant code is enriched via the Upstox error registry so callers
    receive the correct HTTP status and remediation hint.
    """
    failed = [p for p in placement_results if str(p.get("status") or "").upper() != "SUCCESS"]
    codes: list[str] = []
    for p in failed:
        err = p.get("error") or {}
        code = err.get("error_code") if isinstance(err, dict) else None
        if code:
            codes.append(str(code))

    broker_codes = [c for c in codes if c.startswith("UDAPI")]
    dominant = broker_codes[0] if broker_codes else (codes[0] if codes else None)

    if dominant:
        meta = lookup_upstox_error(dominant)
        # If the dominant code is missing from our registry, fall back to the
        # first placement's own enriched error dict.
        if meta is None:
            for p in failed:
                err = p.get("error") or {}
                if isinstance(err, dict) and err.get("error_code") == dominant:
                    return {**err}
        return {
            "error_code": dominant,
            "category": meta.category if meta else "broker",
            "http_status": meta.http_status if meta else None,
            "title": meta.title if meta else None,
            "message": meta.message if meta else "Upstox rejected the order.",
            "remediation": meta.remediation if meta else None,
        }

    return {
        "error_code": "ALL_ORDERS_FAILED",
        "category": "order_placement",
        "http_status": 502,
        "title": "All orders failed",
        "message": "None of the selected option orders was successfully placed.",
        "remediation": None,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
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
        "execute=%s | max_orders=%s | lot=%s | tx=%s",
        clean_key, side, minimum, maximum, execute, max_orders, lot, tx_type,
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

    underlying_ltp = chain.get("underlying_ltp")
    try:
        underlying_ltp_f = float(underlying_ltp) if underlying_ltp is not None else None
    except (TypeError, ValueError):
        underlying_ltp_f = None

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

            trading_symbol = _pick_contract_field(contract, *_SYMBOL_KEYS)
            if not trading_symbol:
                trading_symbol = _derive_trading_symbol(contract, symbol, side)

            lot_size, lot_source = _resolve_lot_size(contract, symbol)

            exchange = _pick_contract_field(contract, *_EXCHANGE_KEYS)
            if not exchange:
                exchange, _ = _resolve_exchange(instrument_key)

            margin_info = _compute_lot_margin(
                ltp=ltp,
                lot_size=lot_size,
                lots=lot,
                transaction_type=tx_type,
                underlying_ltp=underlying_ltp_f,
                strike_price=strike_price,
            )

            matches.append({
                "instrument_key": instrument_key,
                "instrument_type": contract.get("instrument_type") or side,
                "trading_symbol": trading_symbol,
                "expiry": contract.get("expiry"),
                "strike_price": contract.get("strike_price"),
                "moneyness": contract.get("moneyness"),
                "strike_offset": contract.get("strike_offset"),
                "ltp": round(ltp, 2),
                "volume": market_data.get("volume"),
                "oi": market_data.get("oi"),
                "lot_size": lot_size,
                "lot_size_source": lot_source,
                "exchange": exchange,
                "premium_per_unit": margin_info["premium_per_unit"],
                "premium_per_lot": margin_info["premium_per_lot"],
                "premium_total": margin_info["premium_total"],
                "required_margin": margin_info["required_margin"],
                "margin_basis": margin_info["margin_basis"],
                "_midpoint_distance": abs(ltp - ((minimum + maximum) / 2.0)),
                "_atm_distance": atm_distance,
                "_strike_sort": strike_price,
            })

    missing_symbol = sum(1 for m in matches if not m.get("trading_symbol"))
    missing_lot = sum(1 for m in matches if not m.get("lot_size"))
    if missing_symbol or missing_lot:
        logger.warning(
            "Budget option enrichment incomplete | matches=%s | missing_symbol=%s | "
            "missing_lot_size=%s | source=option_chain",
            len(matches), missing_symbol, missing_lot,
        )

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

    selected_margin_total = sum((m.get("required_margin") or 0.0) for m in selected)
    selected_premium_total = sum((m.get("premium_total") or 0.0) for m in selected)
    margin_complete = all(m.get("required_margin") is not None for m in selected)

    response: dict[str, Any] = {
        "status": "PREVIEW" if selected and not execute else "NO_MATCHES" if not selected else "EXECUTING",
        "mode": "EXECUTE" if execute else "DRY_RUN",
        "underlying_key": clean_key,
        "underlying_symbol": chain.get("underlying_symbol") or symbol,
        "underlying_ltp": underlying_ltp,
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
        "margin_summary": {
            "transaction_type": tx_type,
            "lot": lot,
            "selected_contracts_count": len(selected),
            "total_premium": round(selected_premium_total, 2) if margin_complete else None,
            "total_required_margin": round(selected_margin_total, 2) if margin_complete else None,
            "margin_basis": (
                "premium_only_buy" if tx_type == "BUY"
                else "estimated_span_plus_exposure_12pct"
            ),
            "complete": margin_complete,
            "disclaimer": (
                "BUY options: required margin equals the full premium "
                "(ltp × lot_size × lots). SELL options: estimated as "
                "max(premium, 12% of underlying notional) — replace with a "
                "broker SPAN+Exposure calculator for precise short-side margin."
            ),
        },
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
            except Exception:
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
                        "required_margin": blocked_contract.get("required_margin"),
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
                        "category": "service",
                        "http_status": 502,
                        "title": "Invalid order-service response",
                        "message": "Existing place_order_service returned an invalid response.",
                        "remediation": None,
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
                    "category": "service",
                    "http_status": 500,
                    "title": "Order service exception",
                    "message": str(exc),
                    "remediation": None,
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
            "required_margin": contract.get("required_margin"),
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
                "category": "risk_guard",
                "http_status": guard_failure.get("http_status"),
                "title": "Daily profit guard tripped",
                "message": guard_failure["message"],
                "remediation": None,
            }
        else:
            response["step"] = "ORDER_PLACEMENT"
            response["error"] = _aggregate_failure(placement_results)
        response["context"] = {
            "placement_results": placement_results,
            "underlying_key": clean_key,
            "option_type": side,
            "daily_profit_guard": guard_failure,
        }
    return response