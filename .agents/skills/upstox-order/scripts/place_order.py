"""Preview or explicitly confirm a lot-aware Upstox normal AMO order."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "upstox" / "scripts"))
from upstox_helpers import get_client  # noqa: E402


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return to_plain(converter())
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {k: to_plain(v) for k, v in attrs.items() if not k.startswith("_")}
    return str(value)


def emit(value: dict, code: int = 0) -> int:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    return code


def round_to_tick(price: float, tick: float) -> float:
    if not math.isfinite(price) or price <= 0:
        raise ValueError("LIMIT price must be a positive finite number.")
    if not math.isfinite(tick) or tick <= 0:
        raise ValueError(f"Instrument metadata returned invalid tick_size={tick!r}.")
    units = (Decimal(str(price)) / Decimal(str(tick))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return float(units * Decimal(str(tick)))


def fetch_quote(client: Any, instrument_key: str) -> tuple[dict, Any]:
    import upstox_client

    response = upstox_client.MarketQuoteV3Api(client).get_ltp(
        instrument_key=instrument_key
    )
    raw = to_plain(response)
    quotes = raw.get("data", {}) if isinstance(raw, dict) else {}
    if not isinstance(quotes, dict):
        raise ValueError("Upstox LTP response has an unexpected data format.")
    for response_key, quote in quotes.items():
        if (isinstance(quote, dict)
                and str(quote.get("instrument_token", "")) == instrument_key):
            return {"response_key": response_key, **quote}, raw
    returned = [
        str(q.get("instrument_token")) for q in quotes.values()
        if isinstance(q, dict) and q.get("instrument_token") is not None
    ]
    raise ValueError(
        f"Could not find {instrument_key!r} in LTP response. "
        f"Returned instrument_tokens: {returned}"
    )


def fetch_instrument_metadata(client: Any, instrument_key: str, quote: dict | None) -> dict:
    """Resolve exact instrument metadata and lot/tick sizes from Upstox Search."""
    import upstox_client

    prefix = instrument_key.split("|", 1)[0]
    token = instrument_key.split("|", 1)[1] if "|" in instrument_key else ""
    if not token:
        raise ValueError("instrument must be an Upstox key in SEGMENT|IDENTIFIER format.")
    parts = prefix.split("_", 1)
    if len(parts) != 2:
        raise ValueError(f"Unsupported instrument segment {prefix!r}.")
    exchange_code, segment_code = parts
    exchange = {"NCD": "NSE", "BCD": "BSE"}.get(exchange_code, exchange_code)
    if exchange not in {"NSE", "BSE"} or segment_code not in {"FO", "EQ"}:
        raise ValueError(
            f"Automatic metadata/lot validation currently supports NSE/BSE EQ and FO; "
            f"received {prefix!r}."
        )
    segment = segment_code
    response_key = str((quote or {}).get("response_key", ""))
    symbol = response_key.split(":", 1)[1] if ":" in response_key else ""
    if not symbol:
        raise ValueError("Cannot resolve trading symbol for instrument metadata lookup.")

    symbol_upper = symbol.upper()
    underlying_match = re.match(r"^([A-Z]+)", symbol_upper)
    query = underlying_match.group(1) if underlying_match else symbol
    type_match = re.search(r"(CE|PE|FUT)$", symbol_upper)
    instrument_type = type_match.group(1) if type_match else None

    expiry_value = None
    # NSE derivative symbol convention: UNDERLYING + YY + month-code + DD + strike + type.
    expiry_match = re.search(r"(\d{2})([A-Z])(\d{2})\d+(?:CE|PE|FUT)$",
                             symbol_upper)
    month_codes = {
        "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
        "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
        "A": 1, "B": 2, "C": 3, "D": 12,
    }
    if expiry_match and expiry_match.group(2) in month_codes:
        from datetime import date
        try:
            expiry_value = date(
                2000 + int(expiry_match.group(1)),
                month_codes[expiry_match.group(2)],
                int(expiry_match.group(3)),
            ).isoformat()
        except ValueError:
            expiry_value = None

    api = upstox_client.InstrumentsApi(client)
    candidates: dict[str, dict] = {}
    search_errors: list[str] = []
    queries = [query]
    if symbol.upper() != query.upper():
        queries.append(symbol)

    # Expiry is a single date/keyword in this API; never send a comma-separated list.
    expiry_values: list[str | None] = [expiry_value]
    if segment == "FO" and not expiry_value:
        expiry_values.extend(["current_week", "next_week", "current_month",
                              "next_month", "far_month"])

    for search_query in queries:
        for expiry in expiry_values:
            for page in range(1, 101):
                kwargs: dict[str, Any] = {
                    "exchanges": exchange,
                    "segments": segment,
                    "page_number": page,
                    "records": 30,
                }
                if instrument_type:
                    kwargs["instrument_types"] = instrument_type
                if expiry:
                    kwargs["expiry"] = expiry
                try:
                    response = api.search_instrument(search_query, **kwargs)
                    plain = to_plain(response)
                    rows = plain.get("data", []) if isinstance(plain, dict) else []
                    if not isinstance(rows, list):
                        raise ValueError("Instrument Search returned non-list data.")
                    for row in rows:
                        if isinstance(row, dict) and row.get("instrument_key"):
                            candidates[str(row["instrument_key"])] = row
                    page_info = (
                        plain.get("meta_data", {}).get("page", {})
                        if isinstance(plain, dict) else {}
                    )
                    total_pages = int(page_info.get("total_pages") or page)
                    if page >= total_pages or not rows:
                        break
                except Exception as exc:
                    search_errors.append(
                        f"{search_query}/{expiry}/page-{page}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    break
            exact_now = [
                row for row in candidates.values()
                if str(row.get("instrument_key", "")) == instrument_key
                or (str(row.get("exchange_token", "")) == token
                    and str(row.get("segment", "")) == prefix)
            ]
            if exact_now:
                break
        exact_now = [
            row for row in candidates.values()
            if str(row.get("instrument_key", "")) == instrument_key
            or (str(row.get("exchange_token", "")) == token
                and str(row.get("segment", "")) == prefix)
        ]
        if exact_now:
            break

    matches = [
        row for row in candidates.values()
        if str(row.get("instrument_key", "")) == instrument_key
        or (str(row.get("exchange_token", "")) == token
            and str(row.get("segment", "")) == prefix)
    ]
    if len(matches) != 1:
        sample = [
            {k: row.get(k) for k in
             ("instrument_key", "exchange_token", "trading_symbol", "lot_size",
              "tick_size", "expiry")}
            for row in list(candidates.values())[:12]
        ]
        raise ValueError(
            f"Could not uniquely resolve exact metadata for {instrument_key!r}; "
            f"found {len(matches)} exact matches. Search candidates: {sample}. "
            f"Search errors: {search_errors[:3]}"
        )

    item = matches[0]
    try:
        lot_size = int(item.get("lot_size") or 1)
        tick_size = float(item["tick_size"])
    except (TypeError, ValueError, KeyError):
        raise ValueError(
            f"Instrument metadata is missing valid lot_size/tick_size for {instrument_key!r}."
        )
    if lot_size <= 0 or not math.isfinite(tick_size) or tick_size <= 0:
        raise ValueError("Instrument metadata returned invalid lot_size or tick_size.")
    return {
        "instrument_key": item.get("instrument_key"),
        "exchange_token": item.get("exchange_token"),
        "trading_symbol": item.get("trading_symbol", symbol),
        "name": item.get("name"),
        "exchange": item.get("exchange", exchange),
        "segment": item.get("segment", prefix),
        "instrument_type": item.get("instrument_type"),
        "lot_size": lot_size,
        "tick_size": tick_size,
        "freeze_quantity": item.get("freeze_quantity"),
        "expiry": item.get("expiry"),
        "underlying_symbol": item.get("underlying_symbol"),
        "metadata_source": "UPSTOX_INSTRUMENT_SEARCH",
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Preview or explicitly confirm a normal Upstox AMO order."
    )
    p.add_argument("--instrument", required=True,
                   help="Exact Upstox instrument key; it is never guessed.")
    quantity = p.add_mutually_exclusive_group()
    quantity.add_argument("--quantity", type=int, default=None,
                          help="Units; F&O quantity must be a lot-size multiple.")
    quantity.add_argument("--lots", type=int, default=None,
                          help="Number of F&O lots; defaults to one lot.")
    p.add_argument("--order-type", choices=["LIMIT", "MARKET"], default="LIMIT")
    p.add_argument("--price", default="market",
                   help="LIMIT price or 'market' for live LTP (default).")
    p.add_argument("--transaction-type", choices=["BUY", "SELL"], default="BUY")
    p.add_argument("--product", choices=["I", "D", "MTF"], default="D")
    p.add_argument("--validity", choices=["DAY", "IOC"], default="DAY")
    p.add_argument("--amo", dest="is_amo", action="store_true", default=True)
    p.add_argument("--no-amo", dest="is_amo", action="store_false")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--expected-preview-hash")
    return p


def main() -> int:
    args = build_parser().parse_args()
    request = {
        "instrument": args.instrument,
        "quantity": args.quantity,
        "lots": args.lots,
        "order_type": args.order_type,
        "requested_price": args.price,
        "transaction_type": args.transaction_type,
        "product": args.product,
        "validity": args.validity,
        "is_amo": args.is_amo,
    }
    try:
        if args.quantity is not None and args.quantity <= 0:
            raise ValueError("--quantity must be greater than zero.")
        if args.lots is not None and args.lots <= 0:
            raise ValueError("--lots must be greater than zero.")
        if args.confirm and not args.expected_preview_hash:
            raise ValueError(
                "--expected-preview-hash from the reviewed preview is required with --confirm."
            )

        client = get_client()
        # Always resolve the canonical symbol from LTP before metadata lookup.
        quote, quote_raw = fetch_quote(client, args.instrument)
        metadata = fetch_instrument_metadata(client, args.instrument, quote)
        lot_size = metadata["lot_size"]

        if args.lots is not None:
            quantity_value = args.lots * lot_size
            quantity_source = "LOTS"
        elif args.quantity is not None:
            quantity_value = args.quantity
            quantity_source = "EXPLICIT_QUANTITY"
        else:
            quantity_value = lot_size
            quantity_source = "DEFAULT_ONE_LOT"

        if quantity_value <= 0 or quantity_value % lot_size != 0:
            raise ValueError(
                f"Quantity {quantity_value} is invalid for {metadata['trading_symbol']}. "
                f"Instrument lot_size is {lot_size}; quantity must be a positive "
                f"multiple of {lot_size}. Use --lots 1 for one lot."
            )

        if args.order_type == "MARKET":
            if str(args.price).lower() not in {"market", "0", "0.0"}:
                raise ValueError("MARKET orders use price 0; omit --price or use --price 0.")
            price = 0.0
            price_source = "NOT_APPLICABLE_MARKET_ORDER"
        elif str(args.price).lower() == "market":
            if quote is None:
                quote, quote_raw = fetch_quote(client, args.instrument)
            ltp = float(quote["last_price"])
            price = round_to_tick(ltp, metadata["tick_size"])
            price_source = "LIVE_LTP_ROUNDED_TO_TICK"
        else:
            supplied_price = float(args.price)
            price = round_to_tick(supplied_price, metadata["tick_size"])
            price_source = "PROVIDED_ROUNDED_TO_TICK"

        payload = {
            "quantity": quantity_value,
            "product": args.product,
            "validity": args.validity,
            "price": price,
            "tag": "algoapp_order",
            "instrument_token": args.instrument,
            "order_type": args.order_type,
            "transaction_type": args.transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": bool(args.is_amo),
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        preview = {
            "status": "PREVIEW",
            "order": payload,
            "instrument_metadata": metadata,
            "price_source": price_source,
            "reference_ltp": quote.get("last_price") if quote else None,
            "quantity_source": quantity_source,
            "market_quote": quote_raw,
            "preview_hash": fingerprint,
            "confirmation_required": True,
            "amo_note": (
                "AMO is requested by default. Upstox determines whether the request is "
                "treated as AMO from the market session; an AMO request during active "
                "market hours may be rejected or treated as a regular order. Check the "
                "session before confirming."
            ),
            "message": (
                "No order was placed. Review the exact order and preview_hash. "
                "For confirmation, repeat with the resolved --quantity and numeric "
                "--price shown here, plus --confirm and the hash."
            ),
        }
        if not args.confirm:
            return emit(preview)

        if fingerprint != args.expected_preview_hash:
            raise ValueError(
                "Order inputs differ from the reviewed preview hash. No order was placed. "
                "For confirmation, use the exact resolved quantity and numeric order price "
                "from the preview, and keep every other order parameter unchanged."
            )

        token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is not set in the process environment.")
        # Use the project-verified SDK V3 order endpoint and typed request model.
        import upstox_client
        body_model = upstox_client.PlaceOrderV3Request(**payload)
        try:
            response = upstox_client.OrderApiV3(client).place_order(body_model)
            body = to_plain(response)
            return emit({
                "status": "SUCCESS",
                "order": payload,
                "instrument_metadata": metadata,
                "preview_hash": fingerprint,
                "api_response": body,
                "message": (
                    "Upstox accepted the order request. This does not confirm an execution/fill."
                ),
            })
        except Exception as exc:
            status_code = getattr(exc, "status", None)
            raw = getattr(exc, "body", None)
            try:
                detail = json.loads(raw) if isinstance(raw, str) else to_plain(raw)
            except (json.JSONDecodeError, TypeError):
                detail = {"body": str(raw) if raw is not None else str(exc)}
            return emit({
                "status": "API_ERROR",
                "order": payload,
                "instrument_metadata": metadata,
                "preview_hash": fingerprint,
                "http_status": status_code,
                "api_response": detail,
            }, 1)

    except Exception as exc:
        return emit({
            "status": "ERROR",
            "request": request,
            "order": None,
            "preview_hash": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "message": "No order was placed.",
        }, 1)


if __name__ == "__main__":
    raise SystemExit(main())
