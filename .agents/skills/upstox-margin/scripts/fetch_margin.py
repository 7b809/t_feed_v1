"""Calculate Upstox margin and brokerage with instrument-aware defaults."""
from __future__ import annotations

import argparse
import json
import math
import sys
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
        return {str(key): to_plain(item) for key, item in value.items()}
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return to_plain(converter())
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {key: to_plain(item) for key, item in attrs.items()
                if not key.startswith("_")}
    return str(value)


def response_data(response: Any) -> Any:
    plain = to_plain(response)
    return plain.get("data", plain) if isinstance(plain, dict) else plain


def fetch_market_quote(client: Any, instrument_key: str) -> tuple[dict, Any]:
    """Fetch one quote and match by instrument_token, never by response map key."""
    import upstox_client

    response = upstox_client.MarketQuoteV3Api(client).get_ltp(
        instrument_key=instrument_key
    )
    raw = to_plain(response)
    quotes = raw.get("data", {}) if isinstance(raw, dict) else {}
    if not isinstance(quotes, dict) or not quotes:
        raise ValueError("Upstox LTP response did not contain any quote data.")

    for response_key, quote in quotes.items():
        if isinstance(quote, dict) and str(quote.get("instrument_token", "")) == instrument_key:
            return {"response_key": response_key, **quote}, raw

    returned_tokens = [
        str(q.get("instrument_token")) for q in quotes.values()
        if isinstance(q, dict) and q.get("instrument_token") is not None
    ]
    raise ValueError(
        f"Quote not found for {instrument_key!r}. Returned instrument_tokens: "
        f"{returned_tokens}; response keys: {list(quotes.keys())}"
    )


def fetch_instrument_metadata(client: Any, instrument_key: str, quote: dict) -> dict:
    """Resolve lot size using the quote's trading symbol, then verify exact key."""
    import upstox_client

    prefix = instrument_key.split("|", 1)[0]
    parts = prefix.split("_", 1)
    if len(parts) != 2:
        raise ValueError(f"Unrecognized instrument_key segment: {prefix!r}")
    exchange_code, segment_code = parts
    exchange = {"NCD": "NSE", "BCD": "BSE"}.get(exchange_code, exchange_code)
    if exchange not in {"NSE", "BSE", "MCX"}:
        raise ValueError(f"Unsupported exchange prefix for metadata lookup: {exchange_code}")
    segment = "FO" if segment_code in {"FO"} else "EQ" if segment_code == "EQ" else None
    if segment is None:
        raise ValueError(
            f"Margin/brokerage lot-size lookup is not configured for segment {prefix!r}."
        )

    response_key = str(quote.get("response_key", ""))
    if ":" not in response_key:
        raise ValueError("LTP response did not provide an exchange:symbol quote key.")
    trading_symbol = response_key.split(":", 1)[1]
    response = upstox_client.InstrumentsApi(client).search_instrument(
        trading_symbol, exchanges=exchange, segments=segment, records=30
    )
    results = to_plain(response_data(response))
    if not isinstance(results, list):
        raise ValueError("Instrument Search returned an unexpected response format.")

    exact = [
        row for row in results
        if isinstance(row, dict) and row.get("instrument_key") == instrument_key
    ]
    if len(exact) != 1:
        candidates = [
            {"instrument_key": row.get("instrument_key"),
             "trading_symbol": row.get("trading_symbol"),
             "lot_size": row.get("lot_size")}
            for row in results if isinstance(row, dict)
        ]
        raise ValueError(
            f"Could not uniquely resolve metadata for {instrument_key!r} using "
            f"trading symbol {trading_symbol!r}. Exact matches: {len(exact)}; "
            f"candidates: {candidates}"
        )

    item = exact[0]
    try:
        lot_size = int(item["lot_size"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Instrument metadata has no valid lot_size for {instrument_key!r}.")
    if lot_size <= 0:
        raise ValueError(f"Instrument metadata returned invalid lot_size={lot_size}.")
    return {
        "instrument_key": item["instrument_key"],
        "trading_symbol": item.get("trading_symbol", trading_symbol),
        "name": item.get("name"),
        "exchange": item.get("exchange", exchange),
        "segment": item.get("segment", prefix),
        "instrument_type": item.get("instrument_type"),
        "lot_size": lot_size,
        "tick_size": item.get("tick_size"),
        "expiry": item.get("expiry"),
        "underlying_symbol": item.get("underlying_symbol"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate Upstox margin and brokerage estimates."
    )
    parser.add_argument("--kind", choices=["required-margin", "brokerage", "both"],
                        default="both")
    parser.add_argument("--instrument", required=True,
                        help="Exact Upstox instrument_key (required; never inferred).")
    qty = parser.add_mutually_exclusive_group()
    qty.add_argument("--quantity", type=int, default=None,
                     help="Unit quantity. For F&O it must be a multiple of lot size.")
    qty.add_argument("--lots", type=int, default=None,
                     help="Number of F&O lots; converted using live instrument metadata.")
    parser.add_argument("--transaction-type", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--product", choices=["I", "D", "CO", "OCO", "MTF"], default="I")
    parser.add_argument("--price", default="market",
                        help="Numeric price or 'market' (default: live LTP).")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    requested_price = str(args.price)
    result_request = {
        "instrument": args.instrument,
        "quantity": args.quantity,
        "lots": args.lots,
        "transaction_type": args.transaction_type,
        "product": args.product,
        "requested_price": requested_price,
        "price": None,
        "price_source": None,
        "quantity_source": None,
    }

    def emit(payload: dict[str, Any]) -> int:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    try:
        if args.quantity is not None and args.quantity <= 0:
            raise ValueError("--quantity must be greater than zero.")
        if args.lots is not None and args.lots <= 0:
            raise ValueError("--lots must be greater than zero.")

        client = get_client()
        # The quote supplies the canonical trading symbol needed to resolve lot
        # size. It is also the default calculation price when --price is omitted.
        quote, quote_raw = fetch_market_quote(client, args.instrument)
        metadata = fetch_instrument_metadata(client, args.instrument, quote)
        lot_size = metadata["lot_size"]

        if args.lots is not None:
            quantity = args.lots * lot_size
            quantity_source = "LOTS"
        elif args.quantity is not None:
            quantity = args.quantity
            quantity_source = "EXPLICIT_QUANTITY"
        else:
            quantity = lot_size
            quantity_source = "DEFAULT_ONE_LOT"

        if quantity % lot_size != 0:
            lower = (quantity // lot_size) * lot_size
            upper = lower + lot_size
            raise ValueError(
                f"Quantity {quantity} is invalid for {metadata['trading_symbol']}. "
                f"Instrument lot_size is {lot_size}; quantity must be a positive "
                f"multiple of {lot_size}. Valid nearby quantities: "
                f"{lower if lower > 0 else 'none'} or {upper}. Use --lots 1 for "
                f"one lot, or pass --quantity {lot_size}."
            )

        if requested_price.strip().lower() == "market":
            price = float(quote["last_price"])
            price_source = "LIVE_LTP"
        else:
            price = float(requested_price)
            if not math.isfinite(price) or price <= 0:
                raise ValueError("--price must be a positive finite number or 'market'.")
            price_source = "PROVIDED"

        result_request.update({
            "quantity": quantity,
            "price": price,
            "price_source": price_source,
            "quantity_source": quantity_source,
        })

        import upstox_client
        charge_api = upstox_client.ChargeApi(client)
        data: dict[str, Any] = {}
        raw_response: dict[str, Any] = {"market_quote": quote_raw}
        component_errors: list[dict[str, str]] = []

        if args.kind in ("required-margin", "both"):
            try:
                instrument = upstox_client.Instrument(
                    instrument_key=args.instrument,
                    quantity=quantity,
                    transaction_type=args.transaction_type,
                    product=args.product,
                    price=price,
                )
                response = charge_api.post_margin(
                    upstox_client.MarginRequest(instruments=[instrument])
                )
                raw_response["margin"] = to_plain(response)
                data["margin"] = response_data(response)
            except Exception as exc:
                component_errors.append({
                    "component": "margin",
                    "type": type(exc).__name__,
                    "message": str(exc),
                })
                data["margin"] = None

        if args.kind in ("brokerage", "both"):
            try:
                response = charge_api.get_brokerage(
                    instrument_token=args.instrument,
                    quantity=quantity,
                    product=args.product,
                    transaction_type=args.transaction_type,
                    price=price,
                    api_version="2.0",
                )
                raw_response["brokerage"] = to_plain(response)
                data["brokerage"] = response_data(response)
            except Exception as exc:
                component_errors.append({
                    "component": "brokerage",
                    "type": type(exc).__name__,
                    "message": str(exc),
                })
                data["brokerage"] = None

        success_count = sum(
            1 for key in ("margin", "brokerage")
            if key in data and data[key] is not None
        )
        status = ("partial" if success_count and component_errors else
                  "error" if component_errors else "success")
        return emit({
            "status": status,
            "kind": args.kind,
            "request": result_request,
            "instrument_metadata": metadata,
            "data": data,
            "raw_response": raw_response,
            "errors": component_errors,
            "error": None if not component_errors else {
                "type": "CalculationError",
                "message": "One or more calculations failed; see errors.",
            },
        })

    except Exception as exc:
        return emit({
            "status": "error",
            "kind": args.kind,
            "request": result_request,
            "instrument_metadata": None,
            "data": None,
            "raw_response": None,
            "errors": [],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })


if __name__ == "__main__":
    raise SystemExit(main())
